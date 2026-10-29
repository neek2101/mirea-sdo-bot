"""Вход в СДО через единый вход МИРЭА (sso.mirea.ru, Keycloak).

Вход начинаем с /auth/oidc/ самой СДО: Moodle уводит на форму Keycloak, где мы проходим
логин/пароль, код из почты и необязательный шаг «МАКС» (KeycloakAuth). Раньше это делала
pymirea через клиент «Пульса», но с осени 2026 Keycloak требует для него PAR и прямой
вход не пускает. После входа в клиенте остаётся SSO-сессия Keycloak, и дальше обычный
OpenID Connect: открываем /auth/oidc/ СДО, Keycloak видит живую SSO-сессию и без формы
возвращает нас в Moodle (response_mode=form_post) — тот выдаёт cookie MoodleSession.

Пароль нигде не сохраняется. Храним (зашифрованно) только cookie СДО и SSO-сессии:
последние позволяют молча перевойти в СДО, когда истечёт сессия Moodle.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .client import USER_AGENT, StoredCookie, cookies_to_jar, host_of, jar_to_cookies

log = logging.getLogger(__name__)

SSO_HOSTS = ("sso.mirea.ru", "login.mirea.ru")
MAX_STEPS = 8  # сколько страниц Keycloak (обязательные действия и т. п.) готовы пройти за один шаг


class SsoError(Exception):
    pass


@dataclass
class LoginOutcome:
    ok: bool
    message: str = ""
    cookies: list[StoredCookie] = field(default_factory=list)
    need_code: bool = False


def _new_client(proxy: str | None, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        timeout=httpx.Timeout(30.0, connect=10.0),
        proxy=proxy,
        transport=transport,
    )


def _login_action(html: str) -> str | None:
    m = re.search(r'"loginAction"\s*:\s*"(https?://[^"]+)"', html)
    return m.group(1).replace("\\/", "/").replace("\\u0026", "&") if m else None


def _page_id(html: str) -> str:
    m = re.search(r'"pageId"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else ""


def _kc_error(html: str) -> str | None:
    m = re.search(r'"message"\s*:\s*\{[^}]*?"summary"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


# Страницы Keycloak, где нужен код: pageId → (поле с кодом, скрытые поля)
CODE_PAGES = {
    "email-code-form": ("emailCode", {}),  # код из письма на @edu.mirea.ru
    "login-max-otp": ("code", {"login": "true"}),  # код в мессенджере «МАКС»
    "login-otp": ("otp", {}),  # приложение-аутентификатор
}


def _challenge(html: str) -> Any:
    from pymirea import AuthChallenge

    page, action = _page_id(html), _login_action(html)
    if page not in CODE_PAGES or not action:
        return None
    name, hidden = CODE_PAGES[page]
    return AuthChallenge("otp", action, name, dict(hidden))


def _foreign_form(html: str, base_url: str) -> tuple[str, dict[str, str]] | None:
    """Автоотправляемая форма, которой Keycloak возвращает в приложение (response_mode=form_post)."""
    form = BeautifulSoup(html, "html.parser").find("form", action=True)
    if form is None:
        return None
    action = urljoin(base_url, str(form["action"]))
    if host_of(action) in SSO_HOSTS:
        return None
    data = {str(i["name"]): str(i.get("value", "")) for i in form.find_all("input") if i.get("name")}
    return action, data


async def _follow_form_post(client: httpx.AsyncClient, r: httpx.Response) -> httpx.Response:
    form = _foreign_form(r.text, str(r.url)) if host_of(str(r.url)) in SSO_HOSTS else None
    if form is None:
        return r
    return await client.post(form[0], data=form[1], follow_redirects=True)


class KeycloakAuth:
    """Диалог с формами Keycloak МИРЭА. Интерфейс как у pymirea.MireaAuth:
    login / complete_2fa / client / close, результаты — pymirea.AuthResult."""

    def __init__(
        self,
        moodle_url: str,
        *,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.moodle_url = moodle_url.rstrip("/")
        self.client = _new_client(proxy, transport)

    async def login(self, username: str, password: str) -> Any:
        from pymirea import AuthResult

        r = await self.client.get(f"{self.moodle_url}/auth/oidc/", follow_redirects=True)
        if host_of(str(r.url)) not in SSO_HOSTS:
            return AuthResult(False, f"СДО не отправила на единый вход МИРЭА: {host_of(str(r.url))}")
        action = _login_action(r.text)
        if not action:
            log.warning("no loginAction on SSO page %r", _page_id(r.text))
            return AuthResult(False, "Не удалось получить форму входа")
        r = await self.client.post(
            action, data={"username": username, "password": password}, follow_redirects=False
        )
        return await self._advance(r, wrong="Неверный логин или пароль")

    async def complete_2fa(self, challenge: Any, code: str) -> Any:
        from pymirea import AuthResult

        if not code:
            return AuthResult(False, "Введите код подтверждения", challenge=challenge)
        data = {**challenge.hidden_fields, challenge.field_name: code}
        r = await self.client.post(challenge.action_url, data=data, follow_redirects=False)
        result = await self._advance(r, wrong="Неверный код")
        nxt = result.challenge
        if nxt is not None and challenge.field_name == "emailCode" and nxt.field_name == "code":
            # После кода из письма Keycloak МИРЭА показывает шаг «МАКС» (login-max-otp)
            # и принимает на нём тот же код — отправляем его сами, не спрашивая второй раз
            data = {**nxt.hidden_fields, nxt.field_name: code}
            r = await self.client.post(nxt.action_url, data=data, follow_redirects=False)
            result = await self._advance(r, wrong="Нужен код из мессенджера «МАКС»")
        return result

    async def _advance(self, r: httpx.Response, *, wrong: str) -> Any:
        """Пройти страницы Keycloak до конца входа или до вопроса к пользователю."""
        from pymirea import AuthResult

        for _ in range(MAX_STEPS):
            url = str(r.url)
            if r.is_redirect:
                nxt = urljoin(url, r.headers["location"])
                if host_of(nxt) not in SSO_HOSTS:
                    return AuthResult(True, "ok")
                r = await self.client.get(nxt, follow_redirects=False)
                continue
            if host_of(url) not in SSO_HOSTS or _foreign_form(r.text, url):
                return AuthResult(True, "ok")
            html, page = r.text, _page_id(r.text)
            log.info("keycloak page %r", page)
            action = _login_action(html)
            if "required-action" in url and action:
                # Обязательное действие (привязать «МАКС» и т. п.) — пропускаем, как кнопка «Позже»
                skipped = await self.client.post(action, data={"skip": "true"}, follow_redirects=False)
                if not skipped.is_redirect:
                    skipped = await self.client.post(
                        action, data={"cancel-aia": "true"}, follow_redirects=False
                    )
                if skipped.is_redirect:
                    r = skipped
                    continue
            challenge = _challenge(html)
            if challenge is not None:
                # Повторная страница с кодом после ошибки несёт сообщение Keycloak
                ask = (
                    "Пришли код из мессенджера «МАКС»"
                    if page == "login-max-otp"
                    else "Нужен код подтверждения"
                )
                return AuthResult(False, _kc_error(html) or ask, challenge=challenge)
            return AuthResult(False, _kc_error(html) or wrong)
        return AuthResult(False, "Единый вход МИРЭА зациклился, попробуйте позже")

    async def close(self) -> None:
        await self.client.aclose()


async def oidc_hop(client: httpx.AsyncClient, moodle_url: str) -> list[StoredCookie]:
    """Пройти OIDC-редиректы СДО с уже живой SSO-сессией в `client`."""
    moodle_host = host_of(moodle_url)
    r = await client.get(f"{moodle_url}/auth/oidc/", follow_redirects=True)
    r = await _follow_form_post(client, r)
    final_host = host_of(str(r.url))
    if final_host in SSO_HOSTS:
        raise SsoError("Единый вход МИРЭА снова просит логин и пароль")
    if final_host != moodle_host:
        raise SsoError(f"Вход закончился на неожиданном сайте: {final_host}")
    if r.url.path.startswith("/login") or 'id="login"' in r.text:
        raise SsoError("СДО не приняла вход через МИРЭА")
    cookies = jar_to_cookies(client.cookies, [moodle_host, *SSO_HOSTS])
    if not any(c.domain == moodle_host and c.name.startswith("MoodleSession") for c in cookies):
        raise SsoError("СДО не выдала cookie сессии")
    return cookies


class SsoLogin:
    """Одна попытка входа. Живёт от ввода пароля до ввода кода из письма."""

    def __init__(
        self,
        moodle_url: str,
        auth_factory: Callable[[], Any] | None = None,
        *,
        proxy: str | None = None,
    ):
        self.moodle_url = moodle_url.rstrip("/")
        self.auth = auth_factory() if auth_factory else KeycloakAuth(self.moodle_url, proxy=proxy)
        self._challenge: Any = None

    async def start(self, username: str, password: str) -> LoginOutcome:
        result = await self.auth.login(username, password)
        return await self._handle(result)

    async def submit_code(self, code: str) -> LoginOutcome:
        if self._challenge is None:
            return LoginOutcome(False, "Сценарий входа устарел, начните заново")
        result = await self.auth.complete_2fa(self._challenge, code.strip())
        return await self._handle(result)

    async def _handle(self, result: Any) -> LoginOutcome:
        if getattr(result, "challenge", None) is not None:
            self._challenge = result.challenge
            return LoginOutcome(False, result.message or "Нужен код из письма", need_code=True)
        if not result.success:
            return LoginOutcome(False, result.message or "Не удалось войти")
        try:
            cookies = await oidc_hop(self.auth.client, self.moodle_url)
        except (SsoError, httpx.HTTPError) as exc:
            log.warning("oidc hop failed: %s", type(exc).__name__)
            return LoginOutcome(False, f"Вход в МИРЭА прошёл, но СДО не открылась: {exc}")
        return LoginOutcome(True, "ok", cookies)

    async def close(self) -> None:
        try:
            await self.auth.close()
        except Exception:
            log.debug("auth close failed", exc_info=True)


async def silent_relogin(
    moodle_url: str,
    stored: Iterable[StoredCookie],
    *,
    proxy: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[StoredCookie] | None:
    """Новая сессия СДО по сохранённой SSO-сессии, без участия пользователя.

    None — SSO-сессия тоже закончилась, нужен обычный вход.
    """
    sso = [c for c in stored if c.domain in SSO_HOSTS]
    if not sso:
        return None
    async with _new_client(proxy, transport) as client:
        client.cookies = cookies_to_jar(sso)
        try:
            return await oidc_hop(client, moodle_url.rstrip("/"))
        except SsoError as exc:
            log.info("silent relogin impossible: %s", exc)
            return None
