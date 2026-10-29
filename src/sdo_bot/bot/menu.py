"""Основное меню: сроки, оценки, ручное обновление, настройки."""

from __future__ import annotations

import logging
import time
from contextlib import suppress

import httpx
from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..moodle import MoodleError
from ..moodle.models import Activity
from ..render import (
    all_deadlines_view,
    course_deadlines_view,
    course_grades_view,
    grades_overview_view,
    group_by_course,
)
from ..storage import REMINDER_CHOICES, User
from ..sync import NeedLogin
from .common import (
    BTN_BY_COURSE,
    BTN_DEADLINES,
    BTN_GRADES,
    BTN_HELP,
    BTN_SETTINGS,
    BTN_SYNC,
    HELP_TEXT,
    OLD_BUTTONS,
    SETTINGS_LABELS,
    SETTINGS_TEXT,
    login_button,
    settings_keyboard,
    user_menu,
)

log = logging.getLogger(__name__)


async def _active_user(message: Message, app: App) -> User | None:
    user = await app.storage.get_user(message.chat.id)
    if user is None or user.state != "active":
        await message.answer("Сначала нужно войти в СДО.", reply_markup=login_button())
        return None
    return user


# ---- сроки -----------------------------------------------------------------------------


async def _pending(message: Message, app: App) -> tuple[User, list[Activity]] | None:
    user = await _active_user(message, app)
    if user is None:
        return None
    if not user.initialized:
        await message.answer("Ещё загружаю данные СДО, загляни через минуту.")
        return None
    stored = await app.storage.activities(user.tg_id)
    acts = [s.activity for s in stored.values() if s.status == "pending"]
    if user.group_name:
        acts += [t.as_activity() for t in await app.storage.group_tasks(user.group_name, int(time.time()))]
    return user, acts


async def show_deadlines(message: Message, app: App) -> None:
    loaded = await _pending(message, app)
    if loaded is None:
        return
    user, acts = loaded
    for text in all_deadlines_view(acts, int(time.time()), user.last_sync_at):
        await message.answer(text)


async def show_deadline_courses(message: Message, app: App) -> None:
    loaded = await _pending(message, app)
    if loaded is None:
        return
    _, acts = loaded
    if not acts:
        await message.answer("Нет открытых тестов и заданий со сроком.")
        return
    kb = InlineKeyboardBuilder()
    for name, group in group_by_course(acts):
        kb.button(text=f"{name[:55]} ({len(group)})", callback_data=f"d:{group[0].course_id}")
    kb.adjust(1)
    await message.answer("<b>Дедлайны по предметам</b>\n\nВыбери дисциплину.", reply_markup=kb.as_markup())


async def show_course_deadlines(callback: CallbackQuery, app: App) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    loaded = await _pending(message, app)
    if loaded is None:
        return
    user, acts = loaded
    course_id = int((callback.data or "d:0").split(":", 1)[1])
    mine = [a for a in acts if a.course_id == course_id]
    name = mine[0].course_name if mine else await app.storage.course_name(user.tg_id, course_id)
    for text in course_deadlines_view(name or f"Курс {course_id}", mine, int(time.time()), user.last_sync_at):
        await message.answer(text)


# ---- оценки ----------------------------------------------------------------------------


async def show_grades(message: Message, app: App) -> None:
    user = await _active_user(message, app)
    if user is None:
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        async with app.sync.api(user) as api:
            overview = await api.grade_overview()
    except NeedLogin:
        await _session_lost(message, app)
        return
    except (MoodleError, httpx.HTTPError) as exc:
        log.warning("grades for %s failed: %s", user.tg_id, exc)
        await message.answer("😕 СДО сейчас не отвечает, попробуй позже.")
        return
    kb = InlineKeyboardBuilder()
    for g in overview:
        kb.button(text=g.course_name[:60], callback_data=f"g:{g.course_id}")
    kb.adjust(1)
    await message.answer(grades_overview_view(overview), reply_markup=kb.as_markup() if overview else None)


async def show_course_grades(callback: CallbackQuery, app: App) -> None:
    await callback.answer("Загружаю…")
    message = callback.message
    if not isinstance(message, Message):
        return
    user = await _active_user(message, app)
    if user is None:
        return
    course_id = int((callback.data or "g:0").split(":", 1)[1])
    try:
        async with app.sync.api(user) as api:
            items = await api.course_grade_items(course_id)
    except NeedLogin:
        await _session_lost(message, app)
        return
    except (MoodleError, httpx.HTTPError) as exc:
        log.warning("course grades for %s failed: %s", user.tg_id, exc)
        await message.answer("😕 СДО сейчас не отвечает, попробуй позже.")
        return
    name = await app.storage.course_name(user.tg_id, course_id) or f"Курс {course_id}"
    url = f"{app.settings.moodle_url}/grade/report/user/index.php?id={course_id}"
    for text in course_grades_view(name, items, url):
        await message.answer(text)


# ---- ручное обновление ------------------------------------------------------------------


async def manual_sync(message: Message, app: App) -> None:
    user = await _active_user(message, app)
    if user is None:
        return
    progress = await message.answer("🔄 Проверяю СДО…")
    result = await app.scheduler.sync_and_notify(user.tg_id)
    fresh = await app.storage.get_user(user.tg_id)
    if result is None:
        text = (
            "⚠️ Нужно войти заново."
            if fresh is None or fresh.state != "active"
            else "😕 СДО не ответила, попробую позже сама."
        )
    elif result.events:
        text = "✅ Готово — новости выше."
    elif result.errors:
        text = "✅ Проверила, новостей нет. Часть разделов СДО не ответила — догружу позже."
    else:
        text = "✅ Проверила — ничего нового."
    with suppress(TelegramBadRequest):
        await progress.edit_text(text)


# ---- настройки ----------------------------------------------------------------------------


async def show_settings(message: Message, app: App) -> None:
    user = await app.storage.ensure_user(message.chat.id)
    await message.answer(SETTINGS_TEXT, reply_markup=settings_keyboard(user.settings))


async def toggle_setting(callback: CallbackQuery, app: App) -> None:
    user = await app.storage.ensure_user(callback.from_user.id)
    s = user.settings
    kind, value = (callback.data or ":").split(":", 1)
    if kind == "s" and value in SETTINGS_LABELS:
        setattr(s, value, not getattr(s, value))
    elif kind == "r" and value.isdigit() and int(value) in REMINDER_CHOICES:
        h = int(value)
        s.reminders = sorted(set(s.reminders) ^ {h}, reverse=True)
    await app.storage.save_settings(user.tg_id, s)
    await callback.answer("Сохранено")
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=settings_keyboard(s))


async def _session_lost(message: Message, app: App) -> None:
    await app.storage.set_state(message.chat.id, "expired", "сессия истекла")
    await message.answer("⚠️ Сессия СДО закончилась, нужно войти заново.", reply_markup=login_button())


async def show_help(message: Message, app: App) -> None:
    await message.answer(HELP_TEXT, reply_markup=await user_menu(app.storage, message.chat.id))


async def old_button(message: Message, app: App) -> None:
    """Нажата кнопка прежней клавиатуры: выполняем действие и присылаем новую клавиатуру."""
    actions = {BTN_DEADLINES: show_deadlines, BTN_GRADES: show_grades, BTN_SYNC: manual_sync}
    new = OLD_BUTTONS[message.text or ""]
    if new == BTN_SETTINGS:
        await show_settings(message, app)
    else:
        await actions[new](message, app)
    await message.answer(
        "Кнопки меню обновились.", reply_markup=await user_menu(app.storage, message.chat.id)
    )


async def fallback(message: Message, app: App) -> None:
    await message.answer(
        "Не поняла. Пользуйся кнопками меню или /help.",
        reply_markup=await user_menu(app.storage, message.chat.id),
    )


def build_router() -> Router:
    """Новый роутер на каждый вызов: диспетчер можно собрать сколько угодно раз."""
    r = Router(name="menu")
    r.message.register(show_deadlines, F.text == BTN_DEADLINES)
    r.message.register(show_deadlines, Command("deadlines"))
    r.message.register(show_deadline_courses, F.text == BTN_BY_COURSE)
    r.message.register(show_deadline_courses, Command("subjects"))
    r.callback_query.register(show_course_deadlines, F.data.startswith("d:"))
    r.message.register(show_help, F.text == BTN_HELP)
    r.message.register(old_button, F.text.in_(OLD_BUTTONS))
    r.message.register(show_grades, F.text == BTN_GRADES)
    r.message.register(show_grades, Command("grades"))
    r.callback_query.register(show_course_grades, F.data.startswith("g:"))
    r.message.register(manual_sync, F.text == BTN_SYNC)
    r.message.register(manual_sync, Command("sync"))
    r.message.register(show_settings, F.text == BTN_SETTINGS)
    r.message.register(show_settings, Command("settings"))
    r.callback_query.register(toggle_setting, F.data.startswith("s:") | F.data.startswith("r:"))
    r.message.register(fallback, F.text)
    return r
