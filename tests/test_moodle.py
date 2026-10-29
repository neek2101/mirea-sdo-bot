from __future__ import annotations

import httpx
import pytest

from sdo_bot.moodle import MoodleAPI, MoodleClient, MoodleError, SessionExpired, StoredCookie
from sdo_bot.moodle.api import merge_events, parse_course_state, strip_html
from sdo_bot.moodle.parsers import parse_overview, parse_user_report
from sdo_bot.moodle.sso import KeycloakAuth, SsoLogin, oidc_hop, silent_relogin

from .conftest import NOW
from .fakes import MOODLE, FakeMoodle, overview_html, user_report_html

pytestmark = pytest.mark.anyio


# ---- разбор HTML ------------------------------------------------------------------------------


def test_overview_parsing_skips_empty_rows_and_reads_course_ids():
    html = overview_html([(10, "Матанализ &amp; Ко", "85,00"), (11, "Физика", "-")])
    rows = parse_overview(html, MOODLE)
    assert [(r.course_id, r.course_name, r.grade) for r in rows] == [
        (10, "Матанализ & Ко", "85,00"),
        (11, "Физика", "-"),
    ]


def test_user_report_parsing_moodle4_markup():
    html = user_report_html([(1, "Тест", "Тест 1", "8,00"), (2, "Задание", "ДЗ", "-")], "45,50")
    items = parse_user_report(html)
    names = [(i.name, i.grade, i.is_total) for i in items]
    # Тип элемента («Тест»), иконки, меню действий и скрытый текст в значения не попадают
    assert names == [
        ("Тест 1", "8,00", False),
        ("ДЗ", "-", False),
        ("Итоговая оценка за курс", "45,50", True),
    ]
    assert items[0].range == "0–10"
    assert items[0].key == "row_1_7"
    assert not items[1].has_grade


def test_user_report_parsing_moodle3_markup():
    html = (
        '<table class="user-grade"><tr><th class="column-itemname">'
        '<a class="gradeitemheader" href="/mod/assign/view.php?id=5">Эссе</a></th>'
        '<td class="column-grade">Отлично</td><td class="column-range">Шкала</td></tr></table>'
    )
    [item] = parse_user_report(html)
    assert (item.name, item.grade, item.key) == ("Эссе", "Отлично", "/mod/assign/view.php?id=5")


def test_missing_tables_give_empty_results():
    assert parse_overview("<html></html>", MOODLE) == []
    assert parse_user_report("<p>нет</p>") == []


# ---- JSON Moodle -------------------------------------------------------------------------------


def test_merge_events_joins_quiz_open_and_close():
    from .fakes import event

    acts = merge_events(
        [
            event(1, "quiz", 7, "open", 100, "КР", (1, "Курс"), 70),
            event(2, "quiz", 7, "close", 200, "КР", (1, "Курс"), 70),
            event(3, "assign", 8, "due", 150, "ДЗ", (1, "Курс"), 80),
        ],
        MOODLE,
    )
    assert [(a.key, a.open_ts, a.close_ts) for a in acts] == [("assign:8", None, 150), ("quiz:7", 100, 200)]
    assert acts[1].url.endswith("/mod/quiz/view.php?id=70")


def test_parse_course_state_skips_labels_and_hidden():
    mods = parse_course_state(
        5,
        {
            "section": [{"id": "77", "title": "Лекции", "cmlist": ["1", "2", "3"]}],
            "cm": [
                {
                    "id": "1",
                    "name": "Файл",
                    "module": "resource",
                    "modname": "Файл",
                    "url": "u1",
                    "sectionid": "77",
                },
                {"id": "2", "name": "Метка", "module": "label", "modname": "Пояснение", "sectionid": "77"},
                {"id": "3", "name": "Скрытый", "module": "page", "url": "u3", "uservisible": False},
            ],
        },
    )
    assert [(m.cmid, m.modname, m.section) for m in mods] == [(1, "resource", "Лекции")]


def test_strip_html():
    assert strip_html("<p>Привет&nbsp;<b>мир</b></p><p>!</p>") == "Привет мир\n!"


# ---- клиент ------------------------------------------------------------------------------------


def _client(moodle: FakeMoodle, session: str = "good-session") -> MoodleClient:
    return MoodleClient(
        MOODLE, [StoredCookie("MoodleSession", session, "online-edu.mirea.ru")], transport=moodle.transport()
    )


async def test_expired_session_detected_by_redirect(moodle: FakeMoodle):
    async with _client(moodle, "dead") as c:
        with pytest.raises(SessionExpired):
            await c.ensure_sesskey()


async def test_batch_continues_after_moodle_stops_on_error(moodle: FakeMoodle):
    moodle.fail_methods = {"core_courseformat_get_state:20"}
    moodle.contents[30] = [{"name": "S", "modules": [{"id": 9, "name": "x", "modname": "page", "url": "u"}]}]
    async with _client(moodle) as c:
        res = await MoodleAPI(c).course_modules([10, 20, 30])
    assert isinstance(res[20], MoodleError)
    assert [m.cmid for m in res[10]] == [1001, 1002]
    assert [m.cmid for m in res[30]] == [9]  # вызов после ошибки дослан вторым запросом
    assert moodle.ajax_requests == 2


async def test_auth_error_inside_ajax_raises_session_expired(moodle: FakeMoodle):
    async with _client(moodle) as c:
        await c.ensure_sesskey()
        moodle.kill_sessions()
        with pytest.raises(SessionExpired):
            await c.call("core_webservice_get_site_info")


async def test_activities_paginate(moodle: FakeMoodle):
    from .fakes import event

    moodle.events = [event(i, "assign", i, "due", NOW + i, f"ДЗ {i}", (1, "К"), i) for i in range(1, 121)]
    async with _client(moodle) as c:
        acts = await MoodleAPI(c).activities(NOW - 10)
    assert len(acts) == 120


# ---- SSO ---------------------------------------------------------------------------------------


async def test_oidc_hop_turns_sso_session_into_moodle_session(moodle: FakeMoodle):
    async with httpx.AsyncClient(transport=moodle.transport()) as client:
        client.cookies.set("KEYCLOAK_IDENTITY", "sso-identity", domain="sso.mirea.ru", path="/realms/mirea/")
        cookies = await oidc_hop(client, MOODLE)
    moodle_cookie = next(c for c in cookies if c.name == "MoodleSession")
    assert moodle_cookie.value in moodle.valid_sessions and moodle_cookie.value != "anon"
    assert any(c.name == "KEYCLOAK_IDENTITY" for c in cookies)


async def test_silent_relogin_none_when_sso_expired(moodle: FakeMoodle):
    moodle.sso_sessions.clear()
    stored = [StoredCookie("KEYCLOAK_IDENTITY", "sso-identity", "sso.mirea.ru", "/realms/mirea/")]
    assert await silent_relogin(MOODLE, stored, transport=moodle.transport()) is None


class FakeAuth:
    """Повторяет публичный интерфейс pymirea.MireaAuth: login / complete_2fa / client / close."""

    def __init__(self, transport: httpx.MockTransport):
        self.client = httpx.AsyncClient(transport=transport)
        self.closed = False

    async def login(self, username, password):
        from pymirea import AuthChallenge, AuthResult

        if password != "right":
            return AuthResult(False, "Неверное имя пользователя или пароль.")
        return AuthResult(False, "Требуется код", challenge=AuthChallenge("otp", "a", "emailCode", {}))

    async def complete_2fa(self, challenge, code):
        from pymirea import AuthResult

        if code != "123456":
            return AuthResult(False, "Неверный код", challenge=challenge)
        self.client.cookies.set(
            "KEYCLOAK_IDENTITY", "sso-identity", domain="sso.mirea.ru", path="/realms/mirea/"
        )
        return AuthResult(True, "ok", cookies={"KEYCLOAK_IDENTITY": "sso-identity"})

    async def close(self):
        self.closed = True
        await self.client.aclose()


async def test_sso_login_with_email_code(moodle: FakeMoodle):
    login = SsoLogin(MOODLE, auth_factory=lambda: FakeAuth(moodle.transport()))
    assert (await login.start("ivanov@edu.mirea.ru", "wrong")).message.startswith("Неверное")
    first = await login.start("ivanov@edu.mirea.ru", "right")
    assert first.need_code and not first.ok
    retry = await login.submit_code("000000")
    assert retry.need_code
    done = await login.submit_code(" 123456 ")
    assert done.ok
    assert {c.name for c in done.cookies} >= {"MoodleSession", "KEYCLOAK_IDENTITY"}
    await login.close()


async def test_keycloak_login_from_moodle_with_email_code(moodle: FakeMoodle):
    """Настоящий вход: форма Keycloak из /auth/oidc/ СДО, код из письма, возврат через form_post."""
    moodle.sso_sessions.clear()
    login = SsoLogin(MOODLE, auth_factory=lambda: KeycloakAuth(MOODLE, transport=moodle.transport()))
    assert (await login.start("ivanov@edu.mirea.ru", "wrong")).message.startswith("Неверное")
    first = await login.start("ivanov@edu.mirea.ru", "right")
    assert first.need_code and not first.ok
    retry = await login.submit_code("000000")
    assert retry.need_code and retry.message == "Неверный код."
    done = await login.submit_code("123456")
    assert done.ok, done.message
    assert {c.name for c in done.cookies} >= {"MoodleSession", "KEYCLOAK_IDENTITY"}
    await login.close()


async def test_http_errors_do_not_leak_sesskey(moodle: FakeMoodle):
    moodle.fail_pages = {"/lib/ajax/service.php"}
    async with _client(moodle) as c:
        with pytest.raises(MoodleError) as err:
            await c.call("core_webservice_get_site_info")
    assert moodle.sesskey not in str(err.value) and "500" in str(err.value)
