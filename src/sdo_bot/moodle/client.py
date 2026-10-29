"""Низкоуровневый доступ к Moodle от имени пользователя.

В СДО МИРЭА мобильный веб-сервис (login/token.php) выключен, поэтому ходим тем же путём,
что и сама страница: cookie сессии → sesskey со страницы /my/ → lib/ajax/service.php.
Так работают и другие проекты сообщества для online-edu.mirea.ru.

Функции, которые нельзя вызвать через AJAX (отчёты об оценках), читаем как HTML-страницы.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
SESSKEY_RE = re.compile(r'"sesskey"\s*:\s*"([^"]+)"')
# Коды ошибок Moodle, которые означают «сессии больше нет»
AUTH_ERRORS = frozenset(
    {"servicerequireslogin", "invalidsesskey", "requireloginerror", "sessionerroruser", "sessiontimedout"}
)


class MoodleError(Exception):
    """Moodle ответил ошибкой, не связанной со входом."""

    def __init__(self, message: str, errorcode: str = ""):
        super().__init__(message)
        self.errorcode = errorcode


class SessionExpired(MoodleError):
    """Сессия СДО недействительна: нужен повторный вход."""


@dataclass(frozen=True)
class StoredCookie:
    name: str
    value: str
    domain: str
    path: str = "/"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value, "domain": self.domain, "path": self.path}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredCookie:
        return cls(d["name"], d["value"], d["domain"], d.get("path") or "/")


def cookies_to_jar(cookies: Iterable[StoredCookie]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c.name, c.value, domain=c.domain, path=c.path)
    return jar


def jar_to_cookies(jar: httpx.Cookies, hosts: Iterable[str]) -> list[StoredCookie]:
    """Куки из jar, относящиеся к перечисленным хостам (с учётом поддоменов)."""
    hosts = [h.lower() for h in hosts]
    out = []
    for c in jar.jar:
        domain = c.domain.lstrip(".").lower()
        if any(domain == h or h.endswith("." + domain) for h in hosts):
            out.append(StoredCookie(c.name, c.value, domain, c.path or "/"))
    return out


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class MoodleClient:
    """Сессия одного пользователя. Использовать как async context manager."""

    def __init__(
        self,
        base_url: str,
        cookies: Iterable[StoredCookie],
        *,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.host = host_of(self.base_url)
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies_to_jar(cookies),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,  # редирект на страницу входа = сессия закончилась
            proxy=proxy,
            transport=transport,
        )
        self._sesskey: str | None = None

    async def __aenter__(self) -> MoodleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    def export_cookies(self) -> list[StoredCookie]:
        """Актуальные куки СДО (Moodle может их обновить по ходу работы)."""
        return jar_to_cookies(self._http.cookies, [self.host])

    # ---- страницы -------------------------------------------------------------------------

    async def get_page(self, path: str, params: dict[str, Any] | None = None) -> str:
        r = await self._http.get(path, params=params)
        if r.is_redirect:
            target = r.headers.get("location", "")
            if "/login/" in target or "/auth/" in target or host_of(target) not in ("", self.host):
                raise SessionExpired("СДО перенаправила на страницу входа")
            # Внутренний редирект СДО (например, на канонический адрес) — идём следом один раз
            r = await self._http.get(target)
            if r.is_redirect:
                raise SessionExpired("СДО перенаправила на страницу входа")
        if r.status_code in (401, 403):
            raise SessionExpired(f"СДО ответила {r.status_code}")
        _check_status(r)
        return r.text

    async def ensure_sesskey(self) -> str:
        if self._sesskey is None:
            html = await self.get_page("/my/")
            m = SESSKEY_RE.search(html)
            if not m or 'id="login"' in html:
                raise SessionExpired("На странице СДО нет sesskey: вход не выполнен")
            self._sesskey = m.group(1)
        return self._sesskey

    # ---- AJAX -----------------------------------------------------------------------------

    async def call(self, method: str, args: dict[str, Any] | None = None) -> Any:
        [result] = await self.batch([(method, args or {})])
        if isinstance(result, Exception):
            raise result
        return result

    async def batch(self, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Несколько вызовов за один HTTP-запрос.

        Moodle прекращает обработку пачки на первой ошибке, поэтому после ошибки
        досылаем оставшиеся вызовы следующим запросом. Возвращает список той же длины:
        данные или исключение MoodleError на месте каждого вызова. SessionExpired
        пробрасывается сразу — без сессии продолжать бессмысленно.
        """
        sesskey = await self.ensure_sesskey()
        results: list[Any] = [None] * len(calls)
        start = 0
        while start < len(calls):
            chunk = calls[start:]
            payload = [{"index": i, "methodname": m, "args": a} for i, (m, a) in enumerate(chunk)]
            info = ",".join(sorted({m for m, _ in chunk}))[:200]
            r = await self._http.post(
                "/lib/ajax/service.php",
                params={"sesskey": sesskey, "info": info},
                content=json.dumps(payload),
                headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            if r.is_redirect or r.status_code in (401, 403):
                raise SessionExpired("СДО отклонила AJAX-запрос")
            _check_status(r)
            body = r.json()
            if isinstance(body, dict):  # ошибка до обработки вызовов
                raise _error_from(body.get("exception") or body, "ajax")
            if not body:
                results[start] = MoodleError(f"{chunk[0][0]}: пустой ответ")
                start += 1
                continue
            for i, res in enumerate(body):
                if res.get("error"):
                    err = _error_from(res.get("exception") or {}, chunk[i][0])
                    if isinstance(err, SessionExpired):
                        raise err
                    results[start + i] = err
                else:
                    results[start + i] = res.get("data")
            start += max(len(body), 1)
        return results


def _check_status(r: httpx.Response) -> None:
    """Как raise_for_status, но без URL в тексте ошибки: в нём sesskey, а текст ошибки
    попадает в журнал и в базу."""
    if r.status_code >= 400:
        raise MoodleError(f"СДО ответила {r.status_code} на {r.request.url.path}", str(r.status_code))


def _error_from(exc: dict[str, Any], method: str) -> MoodleError:
    code = str(exc.get("errorcode") or "")
    msg = str(exc.get("message") or code or "ошибка Moodle")
    cls = SessionExpired if code in AUTH_ERRORS else MoodleError
    return cls(f"{method}: {msg}", code)
