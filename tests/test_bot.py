"""Сквозной тест: апдейты Telegram → диспетчер aiogram → поддельные СДО и SSO."""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from sdo_bot.app import App
from sdo_bot.bot import build_dispatcher
from sdo_bot.bot.common import BTN_BY_COURSE, BTN_DEADLINES, BTN_GRADES
from sdo_bot.config import Settings
from sdo_bot.moodle.sso import SsoLogin
from sdo_bot.scheduler import Notifier, Scheduler

from .fakes import MOODLE
from .test_moodle import FakeAuth

pytestmark = pytest.mark.anyio
TG = 4242


class RecordingSession(BaseSession):
    """Отвечает Telegram-методам правдоподобно и запоминает, что бот отправил."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1000)

    # Сигнатура задана aiogram.BaseSession
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            # Настоящая сессия привязывает ответы к боту (для message.edit_text и т.п.)
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=TG, type="private"),
                text=method.text,
            ).as_(bot)
        return True

    async def stream_content(self, *a: Any, **kw: Any):  # pragma: no cover
        yield b""

    async def close(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage | EditMessageText)]

    def take(self) -> list[TelegramMethod[Any]]:
        out, self.requests = self.requests, []
        return out


@pytest.fixture
async def env(storage, vault, sync, moodle):
    session = RecordingSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, storage, retry_delay=0)
    scheduler = Scheduler(sync, storage, notifier, interval_minutes=15, concurrency=1)
    settings = Settings(bot_token="42:TEST", secret_key="x" * 40, moodle_url=MOODLE)
    app = App(
        settings=settings,
        storage=storage,
        vault=vault,
        sync=sync,
        notifier=notifier,
        scheduler=scheduler,
        sso_factory=lambda: SsoLogin(MOODLE, auth_factory=lambda: FakeAuth(moodle.transport())),
    )
    dp = build_dispatcher()
    counter = itertools.count(1)
    user = User(id=TG, is_bot=False, first_name="Иван")

    async def say(text: str) -> None:
        msg = Message(
            message_id=next(counter),
            date=datetime.now(),
            chat=Chat(id=TG, type="private"),
            from_user=user,
            text=text,
        )
        await dp.feed_update(bot, Update(update_id=next(counter), message=msg), app=app)

    async def press(data: str) -> None:
        bot_msg = Message(message_id=999, date=datetime.now(), chat=Chat(id=TG, type="private"), text="…")
        cq = CallbackQuery(
            id=str(next(counter)), from_user=user, chat_instance="ci", data=data, message=bot_msg
        )
        await dp.feed_update(bot, Update(update_id=next(counter), callback_query=cq), app=app)

    yield session, say, press, app
    await bot.session.close()


async def test_full_login_flow_and_menu(env, storage):
    session, say, press, _ = env

    await say("/start")
    [start] = session.take()
    assert "Привет" in start.text and start.reply_markup.inline_keyboard[0][0].callback_data == "login"

    await press("login")
    assert any("Введи логин" in t for t in session.texts())
    session.take()

    await say("ivanov.i.i@edu.mirea.ru")
    assert "пароль" in session.texts()[-1]
    session.take()

    await say("right")  # пароль
    sent = session.take()
    assert any(isinstance(m, DeleteMessage) for m in sent), "сообщение с паролем должно удаляться"
    assert "код подтверждения" in sent[-1].text

    await say("123456")
    texts = session.texts()
    session.take()
    assert any("Вход выполнен: Иванов Иван Иванович" in t for t in texts)
    welcome = next(t for t in texts if "Готово, Иван!" in t)
    assert "Контрольная №1" in welcome and "Тест по механике" in welcome
    assert texts[-1].startswith("Меню")

    user = await storage.get_user(TG)
    assert user.state == "active" and user.initialized
    blob = user.session_blob
    assert "right" not in blob and "good-session" not in blob  # всё зашифровано, пароля нет

    await say(BTN_GRADES)
    sent = session.take()
    overview = next(m for m in sent if isinstance(m, SendMessage))
    assert "Математический анализ — <b>12,00</b>" in overview.text
    assert overview.reply_markup.inline_keyboard[0][0].callback_data == "g:10"

    await press("g:10")
    detail = [t for t in session.texts() if "Контрольная №0" in t]
    session.take()
    assert detail and "<b>8,00</b> из 0–10" in detail[0] and "Итог курса: 12,00" in detail[0]

    await say(BTN_DEADLINES)
    deadlines = "\n".join(session.texts())
    session.take()
    assert "Лабораторная 1" in deadlines
    # Дисциплины по алфавиту, каждая жирным один раз, работы под ней
    assert deadlines.index("<b>Математический анализ</b>") < deadlines.index("<b>Физика</b>")
    assert deadlines.count("<b>Физика</b>") == 1
    assert deadlines.index("Тест по механике") > deadlines.index("<b>Физика</b>")

    await say(BTN_BY_COURSE)
    picker = next(m for m in session.take() if isinstance(m, SendMessage))
    buttons = [row[0] for row in picker.reply_markup.inline_keyboard]
    assert [(b.text, b.callback_data) for b in buttons] == [
        ("Математический анализ (1)", "d:10"),
        ("Физика (2)", "d:20"),
    ]
    await press("d:20")
    physics = "\n".join(session.texts())
    session.take()
    assert "Лабораторная 1" in physics and "Тест по механике" in physics and "Контрольная" not in physics
    # Внутри дисциплины: сначала то, что уже идёт, потом то, что откроется позже
    assert physics.index("Лабораторная 1") < physics.index("Тест по механике")

    await press("r:24")
    assert any(isinstance(m, EditMessageReplyMarkup) for m in session.take())
    assert (await storage.get_user(TG)).settings.reminders == [3]


async def test_wrong_password_and_logout(env, storage):
    session, say, press, _ = env
    await say("/login")
    await say("ivanov.i.i@edu.mirea.ru")
    session.take()
    await say("wrong")
    assert any("Неверное имя пользователя" in t for t in session.texts())
    assert (await storage.get_user(TG)) is None or (await storage.get_user(TG)).state != "active"
    session.take()

    await say("/login")
    await say("ivanov.i.i@edu.mirea.ru")
    await say("right")
    await say("123456")
    session.take()
    await press("logout:yes")
    assert await storage.get_user(TG) is None


async def test_cookie_fallback_login(env, storage):
    session, say, _, _ = env
    await say("/cookie good-session")
    texts = session.texts()
    assert any(isinstance(m, DeleteMessage) for m in session.requests)
    assert any("Вход выполнен" in t for t in texts)
    assert (await storage.get_user(TG)).state == "active"


async def test_start_reactivates_blocked_user_without_login(env, storage):
    """Новый токен бота: старому пользователю новый бот писать не может, пока тот не нажмёт /start."""
    session, say, _, _ = env
    await say("/cookie good-session")
    await storage.set_state(TG, "blocked")
    session.take()
    await say("/start")
    assert (await storage.get_user(TG)).state == "active"
    assert not any("Войти" in t for t in session.texts())


async def test_session_loss_notifies_user_once(env, storage, moodle):
    session, say, _, app = env
    await say("/cookie good-session")
    session.take()
    moodle.kill_sessions()  # и SSO-сессии нет: вход был по cookie
    assert await app.scheduler.sync_and_notify(TG) is None
    assert await app.scheduler.sync_and_notify(TG) is None
    lost = [t for t in session.texts() if "Сессия СДО закончилась" in t]
    assert len(lost) == 1
    assert (await storage.get_user(TG)).state == "expired"


async def test_messages_wait_in_outbox_while_telegram_is_down(env, storage, moodle):
    from aiogram.exceptions import TelegramNetworkError

    from .fakes import event

    session, say, _, app = env
    await say("/cookie good-session")
    session.take()

    real = session.make_request

    async def broken(bot, method, timeout=None):  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramNetworkError(method=method, message="unreachable")
        return await real(bot, method, timeout)

    session.make_request = broken
    moodle.events.append(event(9, "assign", 209, "due", 2_000_000_000, "Новое ДЗ", (20, "Физика"), 2009))
    await app.scheduler.sync_and_notify(TG)
    assert next(i.text for i in await storage.outbox(TG)).startswith("🆕")  # событие не потеряно

    session.make_request = real
    await app.notifier.deliver()
    assert any("Новое ДЗ" in t for t in session.texts())
    assert await storage.outbox(TG) == []


async def test_stale_reminder_is_not_delivered_late(env, storage):
    from sdo_bot.outgoing import Outgoing

    session, say, _, app = env
    await say("/cookie good-session")
    session.take()
    await storage.enqueue(TG, [Outgoing("⏰ устарело", expires_at=1)])
    await app.notifier.deliver(TG)
    assert not any("устарело" in t for t in session.texts())
    assert await storage.outbox(TG) == []


# ---- идеи и предложения -----------------------------------------------------------------------


async def test_ideas_propose_vote_and_spam_limits(env, storage, monkeypatch):
    from sdo_bot.bot import ideas

    session, say, press, _ = env
    await say("/ideas")
    assert "Пока ни одной идеи" in session.texts()[-1]
    session.take()

    await press("i:new")
    assert "Опиши идею" in session.texts()[-1]
    await say("коротко")
    assert "Слишком коротко" in session.texts()[-1]
    await say("Смотрите https://spam.example")
    assert "Ссылки" in session.texts()[-1]
    await say("Напоминать о парах за 10 минут до начала")
    assert "Записала! Твоя идея #1" in "\n".join(session.texts())
    session.take()

    def answers() -> list[str]:
        return [m.text for m in session.take() if isinstance(m, AnswerCallbackQuery) and m.text]

    monkeypatch.setattr(ideas, "VOTE_INTERVAL", 0)
    await press("i:v:1:1:0")
    assert answers() == ["Голос +1 учтён"]
    await press("i:v:1:-1:0")
    edits = [m for m in session.requests if isinstance(m, EditMessageText)]
    assert answers() == ["Голос −1 учтён"]
    assert "рейтинг -1 (за 0, против 1)" in edits[-1].text
    await press("i:v:1:-1:0")
    assert answers() == ["Голос снят"]

    monkeypatch.setattr(ideas, "VOTE_INTERVAL", 60)  # голос только что был — следующий отбрасываем
    await press("i:v:1:1:0")
    assert answers() == ["Не так быстро"]

    # Сразу вторую идею нельзя: пауза между идеями
    await press("i:new")
    assert "Следующую идею можно предложить через" in session.texts()[-1]
    session.take()

    # Чужую идею удалить нельзя, свою — можно
    other = await storage.add_idea(777, "Тёмная тема для расписания", "тёмная тема для расписания")
    await say(f"/delidea {other}")
    assert "только свою" in session.texts()[-1]
    await say("/delidea 1")
    assert "Удалила идею #1" in session.texts()[-1]


async def test_menu_button_cancels_idea_input(env, storage):
    session, say, press, _ = env
    await press("i:new")
    await say(BTN_DEADLINES)
    assert "Отменила ввод идеи" in session.texts()[-1]
    assert (await storage.ideas_page(TG, 0, 10))[1] == 0
