from __future__ import annotations

import pymirea
import pytest

from sdo_bot.moodle import StoredCookie
from sdo_bot.security import Vault
from sdo_bot.storage import Storage
from sdo_bot.sync import SyncService

from .fakes import MOODLE, FakeMoodle, event

SECRET = "test-secret-key-that-is-long-enough-123456"
NOW = 1_800_000_000  # фиксированное «сейчас» для тестов
H = 3600

pymirea.configure(pymirea.Config(session_keys=SECRET))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def storage():
    s = await Storage.open(":memory:")
    yield s
    await s.close()


@pytest.fixture
def vault() -> Vault:
    return Vault(SECRET)


@pytest.fixture
def moodle() -> FakeMoodle:
    m = FakeMoodle()
    m.courses = [
        {"id": 10, "fullname": "Математический анализ", "viewurl": f"{MOODLE}/course/view.php?id=10"},
        {"id": 20, "fullname": "Физика", "viewurl": f"{MOODLE}/course/view.php?id=20"},
    ]
    m.events = [
        # Тест уже идёт: открылся час назад, закроется через 2 часа
        event(1, "quiz", 101, "open", NOW - H, "Контрольная №1", (10, "Математический анализ"), 1001),
        event(2, "quiz", 101, "close", NOW + 2 * H, "Контрольная №1", (10, "Математический анализ"), 1001),
        # Тест откроется завтра
        event(3, "quiz", 102, "open", NOW + 24 * H, "Тест по механике", (20, "Физика"), 2001),
        event(4, "quiz", 102, "close", NOW + 26 * H, "Тест по механике", (20, "Физика"), 2001),
        # Задание со сроком через 3 дня
        event(5, "assign", 201, "due", NOW + 72 * H, "Лабораторная 1", (20, "Физика"), 2002),
    ]
    m.contents = {
        10: [
            {
                "name": "Неделя 1",
                "modules": [
                    {
                        "id": 1001,
                        "name": "Контрольная №1",
                        "modname": "quiz",
                        "modplural": "Тесты",
                        "url": f"{MOODLE}/mod/quiz/view.php?id=1001",
                        "uservisible": True,
                    },
                    {
                        "id": 1002,
                        "name": "Лекция 1.pdf",
                        "modname": "resource",
                        "modplural": "Файлы",
                        "url": f"{MOODLE}/mod/resource/view.php?id=1002",
                        "uservisible": True,
                    },
                    {
                        "id": 1003,
                        "name": "Текст на странице",
                        "modname": "label",
                        "modplural": "Пояснения",
                        "uservisible": True,
                    },
                ],
            }
        ],
        20: [
            {
                "name": "Общее",
                "modules": [
                    {
                        "id": 2001,
                        "name": "Тест по механике",
                        "modname": "quiz",
                        "modplural": "Тесты",
                        "url": f"{MOODLE}/mod/quiz/view.php?id=2001",
                        "uservisible": True,
                    },
                    {
                        "id": 2002,
                        "name": "Лабораторная 1",
                        "modname": "assign",
                        "modplural": "Задания",
                        "url": f"{MOODLE}/mod/assign/view.php?id=2002",
                        "uservisible": True,
                    },
                ],
            }
        ],
    }
    m.overview = [(10, "Математический анализ", "12,00"), (20, "Физика", "-")]
    from .fakes import user_report_html

    m.reports = {
        10: user_report_html(
            [(1001, "Тест", "Контрольная №0", "8,00"), (1004, "Задание", "ДЗ 1", "-")], "12,00"
        ),
        20: user_report_html([(2001, "Тест", "Тест по механике", "-")], "-"),
    }
    m.notifications = [
        {
            "id": 5,
            "subject": "Старое уведомление",
            "smallmessage": "давно",
            "contexturl": f"{MOODLE}/x",
            "timecreated": NOW - 100 * H,
            "component": "mod_forum",
        }
    ]
    return m


@pytest.fixture
def session_cookies() -> list[StoredCookie]:
    return [
        StoredCookie("MoodleSession", "good-session", "online-edu.mirea.ru"),
        StoredCookie("KEYCLOAK_IDENTITY", "sso-identity", "sso.mirea.ru", "/realms/mirea/"),
    ]


@pytest.fixture
def sync(storage: Storage, vault: Vault, moodle: FakeMoodle) -> SyncService:
    return SyncService(storage, vault, moodle_url=MOODLE, lookback_days=30, transport=moodle.transport())


@pytest.fixture
async def logged_in(storage: Storage, vault: Vault, session_cookies: list[StoredCookie]) -> int:
    tg = 555
    await storage.ensure_user(tg)
    await storage.set_session(tg, vault.seal(session_cookies), 7, "Иванов Иван Иванович")
    return tg
