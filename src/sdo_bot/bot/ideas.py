"""Идеи и предложения: общий список для всех пользователей с рейтингом +1/−1.

Защита от спама: длина и осмысленность текста, без ссылок, не чаще одной идеи
в IDEA_COOLDOWN и не больше IDEAS_PER_DAY в сутки, без дублей. Голос у каждого один на идею
(повторное нажатие снимает его), слишком частые нажатия отбрасываем. Автор может удалить
свою идею, администраторы (ADMIN_IDS) — любую.
"""

from __future__ import annotations

import re
import time
from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..storage import Idea
from .common import BTN_IDEAS, MENU_BUTTONS

PAGE_SIZE = 5
MIN_LEN, MAX_LEN = 10, 500
IDEA_COOLDOWN = 5 * 60  # секунд между идеями одного человека
IDEAS_PER_DAY = 3
VOTE_INTERVAL = 0.7  # секунд между голосами одного человека

LINK_RE = re.compile(r"https?://|www\.|t\.me/|\b[\w-]+\.(ru|com|net|org|me|io|xyz)\b|@\w{4,}", re.I)


class IdeaForm(StatesGroup):
    text = State()


class VoteThrottle:
    """Не даём закликивать кнопки голосования. Только в памяти."""

    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    def allow(self, tg_id: int) -> bool:
        now = time.monotonic()
        if now - self._last.get(tg_id, 0.0) < VOTE_INTERVAL:
            return False
        self._last[tg_id] = now
        if len(self._last) > 10_000:  # не копим память: старые записи не нужны
            self._last = {k: v for k, v in self._last.items() if now - v < VOTE_INTERVAL}
        return True


def normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def check_text(text: str) -> str | None:
    """Причина отказа или None, если текст годится."""
    if len(text) < MIN_LEN:
        return f"Слишком коротко: опиши идею хотя бы в {MIN_LEN} символах."
    if len(text) > MAX_LEN:
        return f"Слишком длинно: уложись в {MAX_LEN} символов (сейчас {len(text)})."
    if LINK_RE.search(text):
        return "Ссылки и упоминания в идеях не принимаю — опиши словами."
    words = re.findall(r"[^\W\d_]{2,}", text)
    if len(words) < 2:
        return "Не похоже на идею: напиши хотя бы пару слов."
    if re.search(r"(.)\1{9,}", text):
        return "Похоже на случайный набор символов, перепиши, пожалуйста."
    return None


async def posting_limit(app: App, tg_id: int) -> str | None:
    """Причина, по которой сейчас нельзя предложить идею, или None."""
    now = int(time.time())
    recent = await app.storage.ideas_posted_since(tg_id, now - 86400)
    if recent and now - recent[0] < IDEA_COOLDOWN:
        wait = (IDEA_COOLDOWN - (now - recent[0]) + 59) // 60
        return f"Следующую идею можно предложить через {wait} мин."
    if len(recent) >= IDEAS_PER_DAY:
        return f"За сутки можно предложить не больше {IDEAS_PER_DAY} идей. Возвращайся завтра."
    return None


# ---- список -----------------------------------------------------------------------------


def ideas_text(ideas: list[Idea], page: int, pages: int, total: int) -> str:
    lines = [
        "<b>Идеи и предложения</b>",
        "Голосуй +1 или −1, чтобы было видно, что нужно людям. Повторное нажатие снимает голос.",
    ]
    if not ideas:
        lines.append("\nПока ни одной идеи. Предложи первую!")
    for idea in ideas:
        sign = f"+{idea.score}" if idea.score > 0 else str(idea.score)
        lines.append(f"\n<b>#{idea.id}</b> · рейтинг {sign} (за {idea.up}, против {idea.down})")
        lines.append(escape(idea.text))
    if pages > 1:
        lines.append(f"\n<i>Страница {page + 1} из {pages} · всего идей: {total}</i>")
    return "\n".join(lines)


def ideas_keyboard(ideas: list[Idea], page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    sizes = []
    for idea in ideas:
        up = "[+1]" if idea.my_vote == 1 else "+1"
        down = "[−1]" if idea.my_vote == -1 else "−1"
        kb.button(text=f"{up} #{idea.id}", callback_data=f"i:v:{idea.id}:1:{page}")
        kb.button(text=f"{down} #{idea.id}", callback_data=f"i:v:{idea.id}:-1:{page}")
        sizes.append(2)
    nav = 0
    if page > 0:
        kb.button(text="Назад", callback_data=f"i:p:{page - 1}")
        nav += 1
    if page + 1 < pages:
        kb.button(text="Дальше", callback_data=f"i:p:{page + 1}")
        nav += 1
    if nav:
        sizes.append(nav)
    kb.button(text="Предложить идею", callback_data="i:new")
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


async def render_page(app: App, viewer: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    ideas, total = await app.storage.ideas_page(viewer, page * PAGE_SIZE, PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    if page >= pages and total:  # страница опустела (идею удалили) — показываем последнюю
        page = pages - 1
        ideas, total = await app.storage.ideas_page(viewer, page * PAGE_SIZE, PAGE_SIZE)
    return ideas_text(ideas, page, pages, total), ideas_keyboard(ideas, page, pages)


async def show_ideas(message: Message, app: App, state: FSMContext) -> None:
    await state.clear()
    text, kb = await render_page(app, message.chat.id, 0)
    await message.answer(text, reply_markup=kb)


async def cb_page(callback: CallbackQuery, app: App) -> None:
    await callback.answer()
    page = int((callback.data or "i:p:0").rsplit(":", 1)[1])
    await _refresh(callback, app, page)


async def cb_vote(callback: CallbackQuery, app: App, throttle: VoteThrottle) -> None:
    _, _, idea_id, value, page = (callback.data or "").split(":")
    if not throttle.allow(callback.from_user.id):
        await callback.answer("Не так быстро")
        return
    result = await app.storage.vote_idea(int(idea_id), callback.from_user.id, int(value))
    if result is None:
        await callback.answer("Эту идею уже удалили")
    else:
        await callback.answer({1: "Голос +1 учтён", -1: "Голос −1 учтён", 0: "Голос снят"}[result])
    await _refresh(callback, app, int(page))


async def _refresh(callback: CallbackQuery, app: App, page: int) -> None:
    if not isinstance(callback.message, Message):
        return
    text, kb = await render_page(app, callback.from_user.id, page)
    with suppress(TelegramBadRequest):  # «message is not modified» — ничего не поменялось
        await callback.message.edit_text(text, reply_markup=kb)


# ---- новая идея ---------------------------------------------------------------------------


async def cb_new(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    limit = await posting_limit(app, callback.from_user.id)
    if limit:
        await callback.message.answer(limit)
        return
    await state.set_state(IdeaForm.text)
    await callback.message.answer(
        f"Опиши идею одним сообщением ({MIN_LEN}–{MAX_LEN} символов, без ссылок).\n\n/cancel — отмена"
    )


async def got_idea(message: Message, app: App, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text in MENU_BUTTONS:
        await state.clear()
        await message.answer("Отменила ввод идеи. Нажми кнопку ещё раз.")
        return
    if text.startswith("/"):
        await message.answer("Это похоже на команду. Пришли текст идеи или /cancel.")
        return
    problem = check_text(text)
    if problem:
        await message.answer(problem + "\n\nПопробуй ещё раз или /cancel.")
        return
    limit = await posting_limit(app, message.chat.id)
    if limit:
        await state.clear()
        await message.answer(limit)
        return
    norm = normalize(text)
    duplicate = await app.storage.idea_by_norm(norm)
    if duplicate:
        await state.clear()
        await message.answer(f"Такая идея уже есть: #{duplicate}. Проголосуй за неё в /ideas.")
        return
    idea_id = await app.storage.add_idea(message.chat.id, text, norm)
    await state.clear()
    await message.answer(f"Записала! Твоя идея #{idea_id}. Удалить её можно командой /delidea {idea_id}.")
    page_text, kb = await render_page(app, message.chat.id, 0)
    await message.answer(page_text, reply_markup=kb)


async def cmd_delidea(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Укажи номер идеи: /delidea 12")
        return
    exists, author = await app.storage.idea_author(int(arg))
    if not exists:
        await message.answer(f"Идеи #{arg} нет.")
        return
    if author != message.chat.id and message.chat.id not in app.settings.admin_ids:
        await message.answer("Удалить можно только свою идею.")
        return
    await app.storage.delete_idea(int(arg))
    await message.answer(f"Удалила идею #{arg}.")


def build_router() -> Router:
    """Новый роутер на каждый вызов: диспетчер можно собрать сколько угодно раз."""
    r = Router(name="ideas")
    r.message.register(show_ideas, F.text == BTN_IDEAS)
    r.message.register(show_ideas, Command("ideas"))
    r.message.register(cmd_delidea, Command("delidea"))
    r.message.register(got_idea, IdeaForm.text, F.text)
    r.callback_query.register(cb_page, F.data.startswith("i:p:"))
    r.callback_query.register(cb_vote, F.data.startswith("i:v:"))
    r.callback_query.register(cb_new, F.data == "i:new")
    return r
