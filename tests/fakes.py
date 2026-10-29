"""Поддельные СДО (Moodle) и единый вход (Keycloak) поверх httpx.MockTransport.

Ответы повторяют форматы Moodle 4.x: lib/ajax/service.php, страницы отчётов об оценках,
редиректы /auth/oidc/ ↔ sso.mirea.ru.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

MOODLE = "https://online-edu.mirea.ru"
SSO = "https://sso.mirea.ru"


def overview_html(rows: list[tuple[int, str, str]]) -> str:
    body = "".join(
        f'<tr class=""><td class="cell c0">'
        f'<a href="{MOODLE}/grade/report/user/index.php?id={cid}&amp;userid=7">'
        f'{name}</a></td><td class="cell c1">{grade}</td></tr>'
        for cid, name, grade in rows
    )
    return (
        '<html><body><table class="generaltable boxaligncenter" id="overview-grade">'
        '<thead><tr><th class="header c0">Название курса</th><th class="header c1">Оценка</th></tr></thead>'
        f'<tbody>{body}<tr class="emptyrow"><td class="cell c0"></td><td class="cell c1"></td></tr>'
        "</tbody></table></body></html>"
    )


def user_report_html(items: list[tuple[int, str, str, str]], total: str) -> str:
    """items: (id, тип, название, оценка). Разметка как в Moodle 4.x."""
    rows = [
        '<tr><th class="level1 levelodd item column-itemname" colspan="4" id="cat_1_7">'
        '<div class="category d-flex"><div class="rowtitle">Курс</div></div></th></tr>'
    ]
    for iid, kind, name, grade in items:
        rows.append(
            f'<tr><th class="level2 leveleven item column-itemname" id="row_{iid}_7">'
            f'<div class="item d-flex align-items-center">'
            f'<div class="me-1"><i class="icon fa fa-x"></i></div>'
            f'<div><span class="d-block text-uppercase small" title="{kind}">{kind}</span>'
            f'<div class="rowtitle"><a href="{MOODLE}/mod/quiz/view.php?id={iid}">{name}</a></div>'
            "</div></div></th>"
            f'<td class="level2 leveleven item column-grade"><div class="d-flex align-items-center">'
            f'<div>{grade}</div><div class="action-menu">Действия с оценкой</div></div></td>'
            f'<td class="level2 leveleven item column-range">0–10</td>'
            f'<td class="level2 leveleven item column-percentage">{grade}</td></tr>'
        )
    rows.append(
        '<tr><th class="level1 levelodd item column-itemname" id="row_99_7">'
        '<div class="courseitem d-flex"><div><span class="d-block text-uppercase small">Итого</span>'
        '<div class="rowtitle">Итоговая оценка за курс<span class="sr-only">скрытое</span></div>'
        "</div></div></th>"
        f'<td class="level1 item column-grade">{total}</td><td class="column-range">0–100</td>'
        '<td class="column-percentage">-</td></tr>'
    )
    return f'<table class="generaltable boxaligncenter user-grade"><tbody>{"".join(rows)}</tbody></table>'


def event(
    eid: int, mod: str, instance: int, etype: str, ts: int, name: str, course: tuple[int, str], cmid: int
) -> dict[str, Any]:
    return {
        "id": eid,
        "name": f"{name} {'открывается' if etype == 'open' else 'закрывается'}",
        "activityname": name,
        "modulename": mod,
        "component": f"mod_{mod}",
        "instance": instance,
        "eventtype": etype,
        "timesort": ts,
        "timestart": ts,
        "course": {"id": course[0], "fullname": course[1]},
        "url": f"{MOODLE}/mod/{mod}/view.php?id={cmid}",
        "action": {"name": "Начать", "actionable": True},
    }


@dataclass
class FakeMoodle:
    sesskey: str = "SK123"
    valid_sessions: set[str] = field(default_factory=lambda: {"good-session"})
    sso_sessions: set[str] = field(default_factory=lambda: {"sso-identity"})
    courses: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    contents: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    overview: list[tuple[int, str, str]] = field(default_factory=list)
    reports: dict[int, str] = field(default_factory=dict)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    fail_methods: set[str] = field(default_factory=set)  # эти AJAX-вызовы вернут ошибку
    fail_pages: set[str] = field(default_factory=set)  # эти страницы ответят 500
    expire_on: str | None = None  # первый запрос к этому пути «убивает» сессии СДО
    ajax_requests: int = 0
    log: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def kill_sessions(self) -> None:
        self.valid_sessions.clear()

    # ---- маршрутизация ---------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.log.append(f"{request.method} {url.host}{url.path}")
        if url.host == "sso.mirea.ru":
            return self._sso(request)
        path = url.path
        if self.expire_on and path == self.expire_on:
            self.expire_on = None
            self.kill_sessions()
        cookie = _cookies(request).get("MoodleSession")
        logged_in = cookie in self.valid_sessions
        if path in self.fail_pages:
            return httpx.Response(500, text="boom")
        if path.startswith("/auth/oidc"):
            return self._oidc(request)
        if path == "/my/":
            if not logged_in:
                return httpx.Response(303, headers={"location": f"{MOODLE}/login/index.php"})
            return httpx.Response(
                200, text=f'<script>M.cfg = {{"sesskey":"{self.sesskey}"}};</script><h1>Dashboard</h1>'
            )
        if path == "/user/profile.php":
            if not logged_in:
                return httpx.Response(303, headers={"location": f"{MOODLE}/login/index.php"})
            return httpx.Response(
                200,
                text="<title>Иванов Иван Иванович: Публичный профиль | СДО</title>"
                '<script>M.cfg = {"sesskey":"x","userId":7};</script>'
                '<div class="page-header-headings"><h1 class="h2">Иванов Иван Иванович</h1></div>',
            )
        if path == "/lib/ajax/service.php":
            return self._ajax(request, logged_in)
        if path == "/grade/report/overview/index.php":
            if not logged_in:
                return httpx.Response(303, headers={"location": f"{MOODLE}/login/index.php"})
            return httpx.Response(200, text=overview_html(self.overview))
        if path == "/grade/report/user/index.php":
            if not logged_in:
                return httpx.Response(303, headers={"location": f"{MOODLE}/login/index.php"})
            cid = int(url.params["id"])
            return httpx.Response(200, text=self.reports.get(cid, "<p>нет таблицы</p>"))
        return httpx.Response(404)

    def _ajax(self, request: httpx.Request, logged_in: bool) -> httpx.Response:
        self.ajax_requests += 1
        calls = json.loads(request.content)
        out = []
        for c in calls:
            if not logged_in:
                out.append(
                    {"error": True, "exception": {"errorcode": "servicerequireslogin", "message": "login"}}
                )
                break
            if request.url.params.get("sesskey") != self.sesskey:
                out.append(
                    {"error": True, "exception": {"errorcode": "invalidsesskey", "message": "sesskey"}}
                )
                break
            method, args = c["methodname"], c["args"]
            if method in self.fail_methods or f"{method}:{args.get('courseid')}" in self.fail_methods:
                out.append(
                    {"error": True, "exception": {"errorcode": "nopermission", "message": "нет доступа"}}
                )
                break  # как настоящий Moodle: дальше пачку не обрабатываем
            out.append({"error": False, "data": self._call(method, args)})
        return httpx.Response(200, json=out)

    def _call(self, method: str, args: dict[str, Any]) -> Any:
        if method == "core_webservice_get_site_info":
            return {"userid": 7, "fullname": "Иванов Иван Иванович", "sitename": "СДО", "release": "4.1"}
        if method == "core_course_get_enrolled_courses_by_timeline_classification":
            return {"courses": self.courses, "nextoffset": 0}
        if method == "core_calendar_get_action_events_by_timesort":
            evs = sorted(
                (e for e in self.events if e["timesort"] >= args["timesortfrom"]), key=lambda e: e["id"]
            )
            after = args.get("aftereventid")
            if after:
                evs = [e for e in evs if e["id"] > after]
            page = evs[: args["limitnum"]]
            return {
                "events": page,
                "firstid": page[0]["id"] if page else 0,
                "lastid": page[-1]["id"] if page else 0,
            }
        if method == "core_courseformat_get_state":
            # contents хранится в форме разделов с модулями; отдаём как Moodle 4.x — JSON-строкой
            sections, cms = [], []
            for n, s in enumerate(self.contents.get(args["courseid"], [])):
                sections.append(
                    {"id": str(n), "title": s["name"], "cmlist": [str(m["id"]) for m in s["modules"]]}
                )
                for m in s["modules"]:
                    cms.append(
                        {
                            "id": str(m["id"]),
                            "name": m["name"],
                            "module": m["modname"],
                            "modname": m.get("modplural", m["modname"]),
                            "url": m.get("url"),
                            "uservisible": m.get("uservisible", True),
                            "sectionid": str(n),
                        }
                    )
            return json.dumps({"course": {}, "section": sections, "cm": cms})
        if method == "message_popup_get_popup_notifications":
            return {"notifications": self.notifications, "unreadcount": 0}
        raise AssertionError(f"unexpected {method}")

    # ---- OIDC / Keycloak --------------------------------------------------------------------

    def _oidc(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.method == "POST":  # response_mode=form_post, как у настоящего Keycloak
            params = httpx.QueryParams(request.content.decode())
        if "code" in params:
            sid = "s-" + secrets.token_hex(4)
            self.valid_sessions.add(sid)
            return httpx.Response(
                303,
                headers={
                    "location": f"{MOODLE}/my/",
                    "set-cookie": f"MoodleSession={sid}; path=/; secure; HttpOnly",
                },
            )
        q = urlencode({"client_id": "online-edu", "redirect_uri": f"{MOODLE}/auth/oidc/", "state": "st"})
        return httpx.Response(
            303,
            headers={
                "location": f"{SSO}/realms/mirea/protocol/openid-connect/auth?{q}",
                "set-cookie": "MoodleSession=anon; path=/",
            },
        )

    def _sso(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        identity = _cookies(request).get("KEYCLOAK_IDENTITY")
        if path.endswith("/authenticate"):
            form = httpx.QueryParams(request.content.decode())
            step = request.url.params["step"]
            if step == "password" and form.get("password") == "right":
                return httpx.Response(200, text=kc_page("email-code-form", "code"))
            if step == "password":
                return httpx.Response(
                    200, text=kc_page("login", "password", error="Неверное имя пользователя или пароль.")
                )
            if step == "code" and form.get("emailCode") != "123456":
                return httpx.Response(200, text=kc_page("email-code-form", "code", error="Неверный код."))
            if step == "code":  # дальше шаг «МАКС», он принимает тот же код
                return httpx.Response(200, text=kc_page("login-max-otp", "max"))
            if form.get("code") != "123456" or form.get("login") != "true":
                return httpx.Response(200, text=kc_page("login-max-otp", "max", error="Неверный код."))
            self.sso_sessions.add("sso-identity")
            identity = "sso-identity"
            headers = {"set-cookie": "KEYCLOAK_IDENTITY=sso-identity; path=/realms/mirea/"}
        else:
            headers = {}
        if identity in self.sso_sessions:
            state = parse_qs(urlsplit(str(request.url)).query).get("state", ["st"])[0]
            form_post = (
                f'<body onload="document.forms[0].submit()"><form method="post" action="{MOODLE}/auth/oidc/">'
                '<input type="hidden" name="code" value="abc">'
                f'<input type="hidden" name="state" value="{state}">'
                "</form></body>"
            )
            return httpx.Response(200, text=form_post, headers=headers)
        return httpx.Response(200, text=kc_page("qr-login", "password"))


def kc_page(page_id: str, step: str, error: str | None = None) -> str:
    """Страница Keycloak-темы МИРЭА: всё нужное лежит в JS-объекте kcContext."""
    message = f'"message": {{ "summary": "{error}", "type": "error" }},' if error else ""
    action = f"{SSO}/realms/mirea/login-actions/authenticate?step={step}"
    ctx = f'{{ {message} "pageId": "{page_id}", "url": {{ "loginAction": "{action}" }} }}'
    return f"<script>const kcContext = {ctx};</script>"


def _cookies(request: httpx.Request) -> dict[str, str]:
    raw = request.headers.get("cookie", "")
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out
