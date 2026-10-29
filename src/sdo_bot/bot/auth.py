"""Вход в СДО: логин → пароль → (код из почты) → готово. Плюс запасной вход по cookie и выход."""

from __future__ import annotations

import logging
from contextlib import suppress
from html import escape

import httpx
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..moodle import MoodleError, SessionExpired, StoredCookie
from ..moodle.client import host_of
from .common import HELP_TEXT, login_button, user_menu

log = logging.getLogger(__name__)


class LoginForm(StatesGroup):
    username = State()
    password = State()
    code = State()


START_TEXT = (
    "👋 Привет! Я слежу за СДО РТУ МИРЭА (online-edu.mirea.ru) и пишу, когда:\n\n"
    "• появляются новые тесты и задания, открываются контрольные;\n"
    "• преподаватель переносит срок;\n"
    "• до срока остаётся мало времени;\n"
    "• в курсе появляются новые материалы или выставлены оценки.\n\n"
    "По кнопке 📊 покажу текущие баллы по каждой дисциплине.\n\n"
    "Для начала войди через единый вход МИРЭА — тем же логином и паролем, что и на сайте."
)


async def cmd_start(message: Message, app: App, state: FSMContext) -> None:
    await state.clear()
    user = await app.storage.ensure_user(message.from_user.id)
    if user.state == "blocked" and user.session_blob:
        # Бот не смог написать (заблокировали или сменили токен бота и в новый ещё не писали) —
        # раз человек снова здесь, сессия СДО по-прежнему годится, вход не нужен
        await app.storage.set_state(user.tg_id, "active")
        user.state = "active"
    if user.state == "active":
        await message.answer(
            "Я на связи и слежу за СДО 👌", reply_markup=await user_menu(app.storage, user.tg_id)
        )
        return
    await message.answer(START_TEXT, reply_markup=login_button())


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


async def cmd_cancel(message: Message, app: App, state: FSMContext) -> None:
    await app.logins.drop(message.from_user.id)
    await state.clear()
    await message.answer("Отменила.")


# ---- вход через SSO -------------------------------------------------------------------------


async def cmd_login(message: Message, state: FSMContext) -> None:
    await _ask_username(message, state)


async def cb_login(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _ask_username(callback.message, state)


async def _ask_username(message: Message, state: FSMContext) -> None:
    await state.set_state(LoginForm.username)
    await message.answer(
        "Введи логин МИРЭА — почту вида <code>ivanov.i.i@edu.mirea.ru</code>.\n\n/cancel — отмена",
        reply_markup=ReplyKeyboardRemove(),
    )


async def got_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username or " " in username or username.startswith("/"):
        await message.answer("Это не похоже на логин. Пришли почту вида ivanov.i.i@edu.mirea.ru")
        return
    await state.update_data(username=username)
    await state.set_state(LoginForm.password)
    await message.answer("Теперь пароль.\n\n🔒 Я не сохраняю его и сразу удалю сообщение с ним из чата.")


async def got_password(message: Message, state: FSMContext, app: App) -> None:
    password = message.text or ""
    with suppress(TelegramBadRequest):
        await message.delete()
    data = await state.get_data()
    progress = await message.answer("⏳ Вхожу через единый вход МИРЭА…")
    login = app.sso_factory()
    try:
        outcome = await login.start(data["username"], password)
    except httpx.HTTPError as exc:
        log.warning("sso login network error: %s", type(exc).__name__)
        await login.close()
        await state.clear()
        await progress.edit_text("😕 Сайт МИРЭА не отвечает. Попробуй чуть позже: /login")
        return
    del password
    if outcome.need_code:
        await app.logins.put(message.from_user.id, login)
        await state.set_state(LoginForm.code)
        await progress.edit_text(
            "📧 МИРЭА отправила код подтверждения на почту. Пришли его сюда.\n\n/cancel — отмена"
        )
        return
    await login.close()
    if not outcome.ok:
        await state.clear()
        await progress.edit_text(f"❌ {escape(outcome.message)}", reply_markup=login_button())
        return
    await state.clear()
    await _finish(message, app, outcome.cookies, progress)


async def got_code(message: Message, state: FSMContext, app: App) -> None:
    tg_id = message.from_user.id
    login = app.logins.get(tg_id)
    if login is None:
        await state.clear()
        await message.answer("Время на ввод кода вышло. Начнём заново?", reply_markup=login_button())
        return
    code = "".join((message.text or "").split())
    progress = await message.answer("⏳ Проверяю код…")
    try:
        outcome = await login.submit_code(code)
    except httpx.HTTPError:
        await progress.edit_text("😕 Сайт МИРЭА не ответил. Пришли код ещё раз.")
        return
    if outcome.need_code:
        await progress.edit_text(f"❌ {escape(outcome.message)}\n\nПопробуй ещё раз или /cancel.")
        return
    await app.logins.drop(tg_id)
    await state.clear()
    if not outcome.ok:
        await progress.edit_text(f"❌ {escape(outcome.message)}", reply_markup=login_button())
        return
    await _finish(message, app, outcome.cookies, progress)


async def _finish(message: Message, app: App, cookies: list[StoredCookie], progress: Message) -> None:
    tg_id = message.chat.id
    try:
        site = await app.activate_session(tg_id, cookies)
    except SessionExpired:
        await progress.edit_text("❌ СДО не приняла вход. Попробуй ещё раз: /login")
        return
    except (MoodleError, httpx.HTTPError, KeyError) as exc:
        log.warning("activate session failed: %s", exc)
        await progress.edit_text("😕 СДО сейчас не отвечает. Попробуй чуть позже: /login")
        return
    await progress.edit_text(f"✅ Вход выполнен: {escape(site.full_name)}. Загружаю данные СДО…")
    result = await app.scheduler.sync_and_notify(tg_id)
    if result is None:
        await message.answer(
            "Данные пока не загрузились — попробую ещё раз при следующей проверке.",
            reply_markup=await user_menu(app.storage, tg_id),
        )
    elif result.initial:
        await message.answer("Меню — внизу 👇", reply_markup=await user_menu(app.storage, tg_id))
    else:
        await message.answer("Снова на связи 👌", reply_markup=await user_menu(app.storage, tg_id))


# ---- запасной вход по cookie ----------------------------------------------------------------


async def cmd_cookie(message: Message, command: CommandObject, app: App, state: FSMContext) -> None:
    """Если вход через SSO сломался: cookie MoodleSession из браузера (DevTools → Cookies)."""
    with suppress(TelegramBadRequest):
        await message.delete()
    value = (command.args or "").strip()
    if not value:
        await message.answer(
            "Запасной способ входа, если обычный не сработал:\n"
            "1. Войди на online-edu.mirea.ru в браузере.\n"
            "2. DevTools → Application → Cookies → скопируй значение <code>MoodleSession</code>.\n"
            "3. Пришли: <code>/cookie значение</code>\n"
            "4. Закрой вкладку, <b>не нажимая «Выход»</b>."
        )
        return
    await state.clear()
    progress = await message.answer("⏳ Проверяю сессию…")
    cookie = StoredCookie("MoodleSession", value, host_of(app.settings.moodle_url))
    await _finish(message, app, [cookie], progress)


# ---- выход ------------------------------------------------------------------------------


async def cmd_logout(message: Message) -> None:
    await message.answer("Выйти и удалить все мои данные о тебе?", reply_markup=_logout_confirm())


async def cb_logout(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Выйти и удалить все мои данные о тебе?", reply_markup=_logout_confirm()
        )


async def cb_logout_yes(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await state.clear()
    await app.logins.drop(callback.from_user.id)
    await app.storage.delete_user(callback.from_user.id)
    await callback.answer("Готово")
    if isinstance(callback.message, Message):
        await callback.message.edit_text("🗑 Данные удалены. Вернуться можно командой /start.")
        await callback.message.answer("До встречи!", reply_markup=ReplyKeyboardRemove())


async def cb_logout_no(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.delete()


def _logout_confirm():
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data="logout:yes")
    kb.button(text="Нет", callback_data="logout:no")
    return kb.as_markup()


def build_router() -> Router:
    """Новый роутер на каждый вызов: диспетчер можно собрать сколько угодно раз."""
    r = Router(name="auth")
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_cancel, Command("cancel"), StateFilter("*"))
    r.message.register(cmd_login, Command("login"))
    r.callback_query.register(cb_login, F.data == "login")
    r.message.register(got_username, LoginForm.username, F.text)
    r.message.register(got_password, LoginForm.password, F.text)
    r.message.register(got_code, LoginForm.code, F.text)
    r.message.register(cmd_cookie, Command("cookie"))
    r.message.register(cmd_logout, Command("logout"))
    r.callback_query.register(cb_logout, F.data == "logout")
    r.callback_query.register(cb_logout_yes, F.data == "logout:yes")
    r.callback_query.register(cb_logout_no, F.data == "logout:no")
    return r
