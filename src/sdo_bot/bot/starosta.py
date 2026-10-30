"""Старосты: заявка с доказательством → ручное одобрение администратором → задания для группы.

В группе один староста и, если он сам его назначит, один заместитель из одногруппников в боте
с теми же правами на задания. Назначить и снять заместителя может только староста.
Пока у группы нет старосты, у всех в меню кнопка «Для старост»; после одобрения она
у одногруппников пропадает, а у старосты и заместителя превращается в «Панель группы».

Группу студента бот определяет сам по СДО (sync.my_group). Староста ставит задания
группе: предмет, название, срок, пояснение. Участники группы получают их как задания СДО:
сообщение о новом задании и о переносе срока, напоминания перед сроком, звёздочку в списках.
Через сутки после срока задание удаляется (Scheduler.send_reminders → purge_group_tasks).
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import suppress
from datetime import datetime, timedelta
from html import escape

import httpx
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..config import MSK
from ..events import DeadlineChanged, Event, NewActivity
from ..moodle import MoodleError
from ..render import activity_line, fmt_dt, render_events, short_name
from ..storage import GroupTask, StarostaRole, User
from ..sync import NeedLogin, already_due_marks
from .common import BTN_GROUP_PANEL, BTN_STAROSTA, MENU_BUTTONS, user_menu

log = logging.getLogger(__name__)

TITLE_MAX = 120
NOTE_MAX = 500
REREQUEST_AFTER = 86400  # после отказа новую заявку можно подать через сутки
NO_COURSE = "Без предмета"


class TaskForm(StatesGroup):
    course = State()
    title = State()
    deadline = State()
    note = State()
    confirm = State()


# ---- заявка и одобрение -------------------------------------------------------------------

MANUAL_REVIEW = (
    "Старост согласовывает администратор бота вручную. Все заявки рассматривает один студент, "
    "поэтому ответ может прийти позже, чем хотелось бы."
)
ROLE_NAMES = {"starosta": "староста", "deputy": "заместитель старосты"}
PULSE_GROUP_URL = "https://pulse.mirea.ru/services/group"
PROOF_HINT = (
    f'Подсказка: открой <a href="{PULSE_GROUP_URL}">свою группу в «Пульсе» РТУ МИРЭА</a> — там '
    "загрузится вся группа и будет подписано, кто староста. Сделай скриншот этой страницы."
)

GUIDE_ABOUT = (
    "<b>Раздел для старост</b>\n\n"
    "<b>Что может староста</b>\n"
    "• Ставить задания своей группе: предмет, название, дедлайн и пояснение — по шагам, кнопками.\n"
    "• Менять у задания название, предмет, срок и пояснение или удалить его.\n"
    "• Смотреть список группы из СДО и кто из одногруппников пользуется ботом.\n"
    "• Назначить заместителя из одногруппников в боте и снять его. Заместитель может всё то же самое, "
    "кроме этого.\n\n"
    "<b>Что видят одногруппники</b>\n"
    "• Сообщение о новом задании и о переносе срока, напоминания перед дедлайном — как для заданий СДО.\n"
    "• Задание со звёздочкой ⭐ во «Всех дедлайнах» и «По предметам», а внизу списка — "
    "сноску с фамилией и именем старосты и номером группы.\n"
    "• Через сутки после дедлайна задание исчезает само.\n\n"
    "Задания видят только одногруппники, которые пользуются ботом. Группу бот определяет сам по СДО."
)
GUIDE_APPLY = (
    "<b>Как стать старостой в боте</b>\n"
    "1. Нажми «Подать заявку».\n"
    "2. Пришли доказательство, что ты староста: например, скриншот списка группы, где это видно.\n"
    f"{PROOF_HINT}\n"
    "3. Ответь, есть ли у тебя заместитель. Если есть — после одобрения добавишь его сам в панели группы.\n\n"
)


class ApplyForm(StatesGroup):
    proof = State()
    deputy = State()


async def _group_of(app: App, user: User) -> str | None:
    """Группа из базы, а если ещё не определена — прямо сейчас по СДО."""
    if user.group_name:
        return user.group_name
    try:
        async with app.sync.api(user) as api:
            courses = await api.courses()
            group = await api.my_group([c.id for c in courses])
    except (NeedLogin, MoodleError, httpx.HTTPError) as exc:
        log.warning("group detection for %s failed: %s", user.tg_id, exc)
        return None
    await app.storage.set_group(user.tg_id, group, int(time.time()))
    return group


async def group_head(app: App, group: str) -> StarostaRole | None:
    """Одобренный староста группы (не заместитель)."""
    return next((r for r in await app.storage.starostas("approved", group) if not r.is_deputy), None)


async def _say(app: App, bot: Bot, tg_id: int, text: str) -> None:
    """Сообщение с клавиатурой, соответствующей роли получателя: так у него обновляется меню."""
    with suppress(TelegramAPIError):
        await bot.send_message(tg_id, text, reply_markup=await user_menu(app.storage, tg_id))


async def request_role(
    message: Message, app: App, state: FSMContext, tg_id: int, username: str | None
) -> None:
    await state.clear()
    user = await app.storage.get_user(tg_id)
    if user is None or user.state != "active":
        await message.answer("Сначала войди в СДО: /login")
        return
    current = await app.storage.starosta(tg_id)
    if current and current.status == "approved":
        role = ROLE_NAMES[current.role]
        await message.answer(
            f"Ты уже {role} группы {escape(current.group_name)}.",
            reply_markup=await user_menu(app.storage, tg_id),
        )
        return
    if current and current.status == "pending":
        await message.answer(f"Заявка уже у администратора, жди ответа.\n\n{MANUAL_REVIEW}")
        return
    if current and current.status == "rejected" and time.time() - current.requested_at < REREQUEST_AFTER:
        await message.answer("Прошлую заявку отклонили. Подать новую можно через сутки.")
        return
    if not app.settings.admin_ids:
        await message.answer("Сейчас некому одобрить заявку: администратор бота не назначен.")
        return
    group = await _group_of(app, user)
    if not group:
        await message.answer("Не получилось определить твою группу по СДО. Попробуй позже.")
        return
    if head := await group_head(app, group):
        await message.answer(
            f"В группе {escape(group)} уже есть староста: {escape(short_name(head.full_name or ''))}.",
            reply_markup=await user_menu(app.storage, tg_id),
        )
        return
    await state.set_state(ApplyForm.proof)
    await state.update_data(group=group, username=username)
    await message.answer(
        f"<b>Заявка на роль старосты группы {escape(group)}</b>\n\n"
        f"{MANUAL_REVIEW}\n\n"
        "Пришли одним сообщением доказательство, что ты староста своей группы: например, "
        "фото или скриншот списка группы, где это видно.\n\n"
        f"{PROOF_HINT}\n\n"
        "/cancel — отмена"
    )


async def cmd_request(message: Message, app: App, state: FSMContext) -> None:
    username = message.from_user.username if message.from_user else None
    await request_role(message, app, state, message.chat.id, username)


async def cb_request(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await request_role(callback.message, app, state, callback.from_user.id, callback.from_user.username)


async def got_proof(message: Message, state: FSMContext) -> None:
    if message.photo:
        await state.update_data(proof=message.photo[-1].file_id, proof_kind="photo")
    elif message.document:
        await state.update_data(proof=message.document.file_id, proof_kind="document")
    await state.set_state(ApplyForm.deputy)
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, есть заместитель", callback_data="st:dep:1")
    kb.button(text="Нет, я один", callback_data="st:dep:0")
    kb.adjust(1)
    await message.answer(
        "Есть ли у тебя заместитель?\n\n"
        "Если да — после одобрения добавишь его сам в панели группы. Если нет — староста в группе "
        "будет один; передумаешь — добавишь заместителя позже. Решать это можешь только ты.",
        reply_markup=kb.as_markup(),
    )


async def proof_not_file(message: Message, state: FSMContext) -> None:
    if (message.text or "") in MENU_BUTTONS:
        await state.clear()
        await message.answer("Отменила заявку. Нажми кнопку ещё раз.")
        return
    await message.answer("Нужно фото или файл с доказательством. Пришли его или нажми /cancel.")


async def cb_deputy_choice(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        wants = (callback.data or "").endswith(":1")
        await _submit(callback.message, app, state, callback.from_user.id, wants_deputy=wants)


async def _submit(message: Message, app: App, state: FSMContext, tg_id: int, *, wants_deputy: bool) -> None:
    data = await state.get_data()
    await state.clear()
    group = data.get("group")
    user = await app.storage.get_user(tg_id)
    if not group or not data.get("proof") or user is None:
        await message.answer("Заявка устарела, начни заново: /starosta")
        return
    if await group_head(app, group):
        await message.answer("Пока ты заполнял заявку, старосту группы уже одобрили.")
        return
    await app.storage.set_starosta(tg_id, group, "pending", int(time.time()), wants_deputy=wants_deputy)
    username = data.get("username")
    contact = f"@{escape(username)}" if username else f"id {tg_id}"
    caption = "\n".join(
        [
            "<b>Заявка: староста</b>",
            "",
            escape(user.full_name or "без имени"),
            f"Группа: <b>{escape(group)}</b>",
            f"Telegram: {contact}",
            "Заместитель: " + ("будет" if wants_deputy else "нет, староста один"),
        ]
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Одобрить", callback_data=f"st:ok:{tg_id}")
    kb.button(text="Отклонить", callback_data=f"st:no:{tg_id}")
    sent = 0
    for admin in app.settings.admin_ids:
        with suppress(TelegramAPIError):
            if data.get("proof_kind") == "photo":
                await message.bot.send_photo(
                    admin, data["proof"], caption=caption, reply_markup=kb.as_markup()
                )
            else:
                await message.bot.send_document(
                    admin, data["proof"], caption=caption, reply_markup=kb.as_markup()
                )
            sent += 1
    if not sent:
        await app.storage.delete_starosta(tg_id)
        await message.answer("Не получилось отправить заявку администратору. Попробуй позже.")
        return
    await message.answer(
        f"Заявка отправлена: староста группы <b>{escape(group)}</b>.\n\n"
        f"{MANUAL_REVIEW} Напишу, как только её рассмотрят."
    )


async def _edit_admin_card(message: Message | None, text: str) -> None:
    if not isinstance(message, Message):
        return
    with suppress(TelegramBadRequest):
        if message.photo or message.document:
            await message.edit_caption(caption=text)
        else:
            await message.edit_text(text)


async def cb_decide(callback: CallbackQuery, app: App) -> None:
    if callback.from_user.id not in app.settings.admin_ids:
        await callback.answer("Это может сделать только администратор", show_alert=True)
        return
    _, verdict, raw = (callback.data or "").split(":")
    tg_id = int(raw)
    req = await app.storage.starosta(tg_id)
    if req is None or req.status != "pending":
        await callback.answer("Заявку уже рассмотрели")
        return
    group = escape(req.group_name)
    name = escape(req.full_name or f"id {tg_id}")
    now = int(time.time())
    if verdict != "ok":
        await app.storage.set_starosta(tg_id, req.group_name, "rejected", now, wants_deputy=req.wants_deputy)
        await callback.answer("Отклонено")
        await _say(app, callback.bot, tg_id, "Заявку отклонили. Если это ошибка, напиши администратору бота.")
        await _edit_admin_card(callback.message, f"Отклонено: {name}, группа {group}.")
        return
    if await group_head(app, req.group_name):
        await callback.answer(f"В группе {req.group_name} староста уже есть", show_alert=True)
        return
    await app.storage.update_starosta_status(tg_id, "approved")
    await callback.answer("Одобрено")
    text = (
        f"Заявку одобрили: теперь ты староста группы <b>{group}</b>.\n\n"
        f"Всё для группы — кнопка «{BTN_GROUP_PANEL}» внизу."
    )
    if req.wants_deputy:
        text += " Там же добавь заместителя: кнопка «Добавить заместителя»."
    await _say(app, callback.bot, tg_id, text)
    await _edit_admin_card(callback.message, f"Одобрено: {name}, староста группы {group}.")
    # Остальные заявки этой группы больше не нужны
    for other in await app.storage.starostas("pending", req.group_name):
        await app.storage.set_starosta(other.tg_id, req.group_name, "rejected", now)
        await _say(app, callback.bot, other.tg_id, "Старосту твоей группы уже одобрили, заявка закрыта.")
    # Одногруппникам — новость и меню без кнопки «Для старост»
    who = escape(short_name(req.full_name or ""))
    for member in await app.storage.group_members(req.group_name):
        if member.tg_id != tg_id:
            await _say(
                app,
                callback.bot,
                member.tg_id,
                f"В группе {group} появился староста: {who}. "
                "Его задания будут в твоих дедлайнах со звёздочкой ⭐.",
            )


async def cmd_list(message: Message, app: App) -> None:
    """Для администратора: реестр старост и заявки, с кнопками снятия."""
    if message.chat.id not in app.settings.admin_ids:
        await message.answer("Команда только для администратора.")
        return
    approved = await app.storage.starostas("approved")
    pending = await app.storage.starostas("pending")

    def line(r: StarostaRole) -> str:
        return f"• {escape(r.group_name)}: {escape(r.full_name or str(r.tg_id))} ({ROLE_NAMES[r.role]})"

    lines = ["<b>Реестр старост</b>"]
    lines += [line(r) for r in approved] or ["Пока никого."]
    if pending:
        lines += ["", "<b>Ждут одобрения</b> (карточки с доказательствами — выше в чате)"]
        lines += [line(r) for r in pending]
    kb = InlineKeyboardBuilder()
    for r in approved:
        kb.button(
            text=f"Снять: {short_name(r.full_name or str(r.tg_id))} ({r.group_name})",
            callback_data=f"st:rm:{r.tg_id}",
        )
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup() if approved else None)


async def cb_remove(callback: CallbackQuery, app: App) -> None:
    if callback.from_user.id not in app.settings.admin_ids:
        await callback.answer("Это может сделать только администратор", show_alert=True)
        return
    tg_id = int((callback.data or "st:rm:0").rsplit(":", 1)[1])
    await app.storage.delete_starosta(tg_id)
    await callback.answer("Снят")
    await _say(app, callback.bot, tg_id, "Тебя сняли с роли старосты в боте.")
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)


# ---- вход в раздел ----------------------------------------------------------------------


async def show_section(message: Message, app: App, state: FSMContext, tg_id: int | None = None) -> None:
    """Кнопка раздела: старосте и заместителю — панель группы, остальным — описание и заявка."""
    await state.clear()
    tg_id = tg_id or message.chat.id
    me = await app.storage.starosta(tg_id)
    if me and me.status == "approved":
        await show_panel(message, app, state, tg_id)
        return
    user = await app.storage.get_user(tg_id)
    head = await group_head(app, user.group_name) if user and user.group_name else None
    if head:  # у группы уже есть староста — кнопка этому человеку не нужна
        await message.answer(
            f"В группе {escape(head.group_name)} староста — {escape(short_name(head.full_name or ''))}. "
            "Его задания видны в твоих дедлайнах со звёздочкой ⭐.",
            reply_markup=await user_menu(app.storage, tg_id),
        )
        return
    kb = InlineKeyboardBuilder()
    can_apply = False
    if me and me.status == "pending":
        status = f"<b>Твоя заявка</b> на рассмотрении.\n{MANUAL_REVIEW}"
    elif me and me.status == "rejected" and time.time() - me.requested_at < REREQUEST_AFTER:
        status = "Прошлую заявку отклонили. Подать новую можно через сутки."
    else:
        status = GUIDE_APPLY + MANUAL_REVIEW
        kb.button(text="Подать заявку", callback_data="st:req")
        can_apply = True
    await message.answer(f"{GUIDE_ABOUT}\n\n{status}", reply_markup=kb.as_markup() if can_apply else None)


async def cb_guide(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        kb = InlineKeyboardBuilder()
        kb.button(text="Назад к панели группы", callback_data="gt:panel")
        await callback.message.answer(GUIDE_ABOUT, reply_markup=kb.as_markup())


# ---- панель группы -------------------------------------------------------------------------


async def _my_group(app: App, tg_id: int, message: Message | None = None) -> str | None:
    group = await app.storage.approved_group(tg_id)
    if group is None and message is not None:
        await message.answer("Это доступно только старосте группы и его заместителю.")
    return group


def panel_view(
    me: StarostaRole, team: list[StarostaRole], tasks: list[GroupTask], now: int
) -> tuple[str, InlineKeyboardMarkup]:
    head = next((r for r in team if not r.is_deputy), None)
    deputy = next((r for r in team if r.is_deputy), None)
    lines = [
        f"<b>Панель группы {escape(me.group_name)}</b>",
        f"Ты {ROLE_NAMES[me.role]}. Здесь ты ставишь задания группе и меняешь их.",
        "",
        f"Староста: {escape(short_name(head.full_name or '')) if head else 'не назначен'}",
        f"Заместитель: {escape(short_name(deputy.full_name or '')) if deputy else 'нет'}",
        "",
        "<b>Задания группы</b>",
    ]
    if not tasks:
        lines.append("Пока нет ни одного задания.")
    for t in tasks:
        when = f"до {fmt_dt(t.deadline)}" if t.deadline > now else f"срок прошёл {fmt_dt(t.deadline)}"
        lines.append(f"⭐ <b>{escape(t.title)}</b>\n    {escape(t.course_name)} · {when}")
    lines += [
        "",
        "Задания видят все в группе, кто пользуется ботом. Через сутки после срока задание исчезает.",
    ]
    kb = InlineKeyboardBuilder()
    for t in tasks:
        kb.button(text=f"Изменить: {t.title[:40]}", callback_data=f"gt:e:{t.id}")
    kb.button(text="Добавить задание", callback_data="gt:new")
    kb.button(text="Список группы", callback_data="gt:roster")
    if not me.is_deputy:  # заместителя назначает и снимает только сам староста
        if deputy:
            kb.button(
                text=f"Снять заместителя: {short_name(deputy.full_name or '')}", callback_data="gt:undep"
            )
        else:
            kb.button(text="Добавить заместителя", callback_data="gt:dep")
    kb.button(text="Как это работает", callback_data="st:guide")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def show_panel(message: Message, app: App, state: FSMContext, tg_id: int | None = None) -> None:
    await state.clear()
    tg_id = tg_id or message.chat.id
    if await _my_group(app, tg_id, message) is None:
        return
    me = await app.storage.starosta(tg_id)
    assert me is not None
    now = int(time.time())
    team = await app.storage.starostas("approved", me.group_name)
    text, kb = panel_view(me, team, await app.storage.group_tasks(me.group_name, now), now)
    await message.answer(text, reply_markup=kb)


async def cb_panel(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_panel(callback.message, app, state, callback.from_user.id)


# ---- заместитель ----------------------------------------------------------------------------


async def _head_only(callback: CallbackQuery, app: App) -> StarostaRole | None:
    me = await app.storage.starosta(callback.from_user.id)
    if me is None or me.status != "approved" or me.is_deputy:
        await callback.answer("Заместителя назначает только староста группы", show_alert=True)
        return None
    return me


async def _deputy_of(app: App, group: str) -> StarostaRole | None:
    return next((r for r in await app.storage.starostas("approved", group) if r.is_deputy), None)


async def cb_add_deputy(callback: CallbackQuery, app: App) -> None:
    me = await _head_only(callback, app)
    message = callback.message
    if me is None or not isinstance(message, Message):
        return
    await callback.answer()
    if await _deputy_of(app, me.group_name):
        await message.answer("Заместитель уже есть. Чтобы назначить другого, сначала сними текущего.")
        return
    candidates = [m for m in await app.storage.group_members(me.group_name) if m.tg_id != me.tg_id]
    if not candidates:
        await message.answer(
            "Заместителем можно назначить одногруппника, который пользуется ботом. Пока таких нет — "
            "пусть откроет бота и войдёт в СДО, тогда он появится здесь."
        )
        return
    kb = InlineKeyboardBuilder()
    for m in sorted(candidates, key=lambda u: (u.full_name or "").casefold()):
        kb.button(text=short_name(m.full_name or str(m.tg_id)), callback_data=f"gt:dep:{m.tg_id}")
    kb.button(text="Отмена", callback_data="gt:panel")
    kb.adjust(1)
    await message.answer(
        "Кого назначить заместителем? Здесь одногруппники, которые пользуются ботом.",
        reply_markup=kb.as_markup(),
    )


async def cb_pick_deputy(callback: CallbackQuery, app: App) -> None:
    me = await _head_only(callback, app)
    message = callback.message
    if me is None or not isinstance(message, Message):
        return
    await callback.answer()
    tg_id = int((callback.data or "").rsplit(":", 1)[1])
    user = await app.storage.get_user(tg_id)
    if user is None or user.group_name != me.group_name:
        await message.answer("Этот человек не из твоей группы.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, назначить", callback_data=f"gt:depok:{tg_id}")
    kb.button(text="Отмена", callback_data="gt:panel")
    await message.answer(
        f"Назначить заместителем: {escape(short_name(user.full_name or ''))}?\n\n"
        "Заместитель сможет ставить, менять и удалять задания группы. Снять его можешь только ты.",
        reply_markup=kb.as_markup(),
    )


async def cb_confirm_deputy(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    me = await _head_only(callback, app)
    message = callback.message
    if me is None or not isinstance(message, Message):
        return
    tg_id = int((callback.data or "").rsplit(":", 1)[1])
    user = await app.storage.get_user(tg_id)
    if user is None or user.group_name != me.group_name or user.state != "active":
        await callback.answer("Этот человек не из твоей группы", show_alert=True)
        return
    if await _deputy_of(app, me.group_name):
        await callback.answer("Заместитель уже есть", show_alert=True)
        return
    await app.storage.set_starosta(tg_id, me.group_name, "approved", int(time.time()), role="deputy")
    await callback.answer("Назначен")
    head, deputy = escape(short_name(me.full_name or "")), escape(short_name(user.full_name or ""))
    group = escape(me.group_name)
    await _say(
        app,
        callback.bot,
        tg_id,
        f"Староста {head} назначил тебя заместителем в группе <b>{group}</b>.\n\n"
        f"Задания группы — кнопка «{BTN_GROUP_PANEL}» внизу.",
    )
    for admin in app.settings.admin_ids:  # администратор видит все изменения реестра
        with suppress(TelegramAPIError):
            await callback.bot.send_message(
                admin, f"Реестр старост: {head} назначил заместителя {deputy} ({group})."
            )
    await show_panel(message, app, state, me.tg_id)


async def cb_remove_deputy(callback: CallbackQuery, app: App) -> None:
    me = await _head_only(callback, app)
    message = callback.message
    if me is None or not isinstance(message, Message):
        return
    await callback.answer()
    deputy = await _deputy_of(app, me.group_name)
    if deputy is None:
        await message.answer("Заместителя нет.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, снять", callback_data="gt:undep!")
    kb.button(text="Отмена", callback_data="gt:panel")
    await message.answer(
        f"Снять заместителя {escape(short_name(deputy.full_name or ''))}? "
        "Его задания останутся у группы до своего срока.",
        reply_markup=kb.as_markup(),
    )


async def cb_confirm_remove_deputy(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    me = await _head_only(callback, app)
    message = callback.message
    if me is None or not isinstance(message, Message):
        return
    deputy = await _deputy_of(app, me.group_name)
    if deputy is not None:
        await app.storage.delete_starosta(deputy.tg_id)
        await _say(app, callback.bot, deputy.tg_id, "Староста снял тебя с роли заместителя в боте.")
    await callback.answer("Снят" if deputy else "Заместителя уже нет")
    await show_panel(message, app, state, me.tg_id)


async def cb_roster(callback: CallbackQuery, app: App) -> None:
    await callback.answer("Загружаю список…")
    message = callback.message
    if not isinstance(message, Message):
        return
    group = await _my_group(app, callback.from_user.id, message)
    user = await app.storage.get_user(callback.from_user.id)
    if group is None or user is None:
        return
    try:
        async with app.sync.api(user) as api:
            courses = await api.courses()
            people = await api.group_roster([c.id for c in courses], group)
    except (NeedLogin, MoodleError, httpx.HTTPError) as exc:
        log.warning("roster for %s failed: %s", user.tg_id, exc)
        await message.answer("СДО сейчас не отвечает, попробуй позже.")
        return
    if not people:
        await message.answer("Не нашла список группы в СДО.")
        return
    in_bot = await app.storage.users_by_moodle_ids(p.moodle_id for p in people)
    active = {mid for mid, u in in_bot.items() if u.state == "active" and u.group_name == group}
    lines = [f"<b>Группа {escape(group)}</b> · {len(people)} чел.", f"Пользуются ботом: {len(active)}", ""]
    for i, p in enumerate(people, 1):
        mark = " — в боте" if p.moodle_id in active else ""
        lines.append(f"{i}. {escape(p.full_name)}{mark}")
    await message.answer("\n".join(lines))


# ---- создание и редактирование ---------------------------------------------------------


def _cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="gt:x")
    return kb.as_markup()


async def _ask_course(message: Message, app: App, state: FSMContext, tg_id: int, step: str) -> None:
    user = await app.storage.get_user(tg_id)
    if user is None:
        return
    try:
        async with app.sync.api(user) as api:
            courses = await api.courses()
    except (NeedLogin, MoodleError, httpx.HTTPError):
        await message.answer("СДО сейчас не отвечает, не могу загрузить список предметов. Попробуй позже.")
        return
    await state.update_data(courses=[[c.id, c.name] for c in courses])
    await state.set_state(TaskForm.course)
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(courses):
        kb.button(text=c.name[:60], callback_data=f"gt:c:{i}")
    kb.button(text=NO_COURSE, callback_data="gt:c:-1")
    kb.button(text="Отмена", callback_data="gt:x")
    kb.adjust(1)
    await message.answer(f"{step}Выбери предмет.", reply_markup=kb.as_markup())


async def cb_new(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message) or await _my_group(app, callback.from_user.id, message) is None:
        return
    await state.clear()
    await state.update_data(mode="new")
    await _ask_course(message, app, state, callback.from_user.id, "<b>Новое задание · шаг 1 из 4</b>\n\n")


async def cb_course(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    message = callback.message
    data = await state.get_data()
    if not isinstance(message, Message) or "courses" not in data:
        return
    idx = int((callback.data or "gt:c:-1").rsplit(":", 1)[1])
    course_id, course_name = data["courses"][idx] if 0 <= idx < len(data["courses"]) else (0, NO_COURSE)
    await state.update_data(course_id=course_id, course_name=course_name)
    if data.get("mode") == "edit":
        await _save_field(
            message, app, state, callback.from_user.id, course_id=course_id, course_name=course_name
        )
        return
    await state.set_state(TaskForm.title)
    await message.answer(
        f"<b>Шаг 2 из 4</b> · {escape(course_name)}\n\nНапиши название задания одним сообщением.",
        reply_markup=_cancel_kb(),
    )


def _deadline_kb() -> InlineKeyboardMarkup:
    now = datetime.now(MSK)
    end = now.replace(hour=23, minute=59, second=0, microsecond=0)
    options = [
        ("Сегодня 23:59", end),
        ("Завтра 23:59", end + timedelta(days=1)),
        ("Через 3 дня", end + timedelta(days=3)),
        ("Через неделю", end + timedelta(days=7)),
    ]
    kb = InlineKeyboardBuilder()
    for label, when in options:
        if when > now:
            kb.button(text=label, callback_data=f"gt:d:{int(when.timestamp())}")
    kb.button(text="Отмена", callback_data="gt:x")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


DEADLINE_HINT = (
    "Нажми кнопку или напиши дату: <code>31.10</code>, <code>31.10 18:00</code> "
    "или <code>31.10.2026 18:00</code>. Без времени — до 23:59."
)


def parse_deadline(text: str, now: datetime) -> datetime | None:
    """«дд.мм», «дд.мм чч:мм», «дд.мм.гггг [чч:мм]» по Москве. Без года — ближайшая такая дата."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:\s+(\d{1,2})[:.](\d{2}))?", text.strip())
    if not m:
        return None
    day, month = int(m[1]), int(m[2])
    year = int(m[3]) if m[3] else now.year
    if year < 100:
        year += 2000
    hour, minute = (int(m[4]), int(m[5])) if m[4] else (23, 59)
    try:
        when = datetime(year, month, day, hour, minute, tzinfo=MSK)
    except ValueError:
        return None
    if not m[3] and when < now:
        when = when.replace(year=year + 1)
    return when


async def got_title(message: Message, app: App, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if await _menu_pressed(message, state):
        return
    if not 3 <= len(title) <= TITLE_MAX:
        await message.answer(f"Название должно быть от 3 до {TITLE_MAX} символов.")
        return
    await state.update_data(title=title)
    if (await state.get_data()).get("mode") == "edit":
        await _save_field(message, app, state, message.chat.id, title=title)
        return
    await state.set_state(TaskForm.deadline)
    await message.answer(
        f"<b>Шаг 3 из 4</b> · Когда дедлайн?\n\n{DEADLINE_HINT}", reply_markup=_deadline_kb()
    )


async def _set_deadline(message: Message, app: App, state: FSMContext, tg_id: int, ts: int) -> None:
    if ts <= time.time():
        await message.answer("Этот момент уже прошёл. Укажи срок в будущем.")
        return
    await state.update_data(deadline=ts)
    if (await state.get_data()).get("mode") == "edit":
        await _save_field(message, app, state, tg_id, deadline=ts)
        return
    await state.set_state(TaskForm.note)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="gt:skip")
    kb.button(text="Отмена", callback_data="gt:x")
    await message.answer(
        f"<b>Шаг 4 из 4</b> · Срок: {fmt_dt(ts)}\n\n"
        f"Добавь пояснение: что именно сделать, где взять материалы (до {NOTE_MAX} символов). "
        "Или нажми «Пропустить».",
        reply_markup=kb.as_markup(),
    )


async def cb_deadline(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        ts = int((callback.data or "gt:d:0").rsplit(":", 1)[1])
        await _set_deadline(callback.message, app, state, callback.from_user.id, ts)


async def got_deadline(message: Message, app: App, state: FSMContext) -> None:
    if await _menu_pressed(message, state):
        return
    when = parse_deadline(message.text or "", datetime.now(MSK))
    if when is None:
        await message.answer(f"Не поняла дату.\n\n{DEADLINE_HINT}")
        return
    await _set_deadline(message, app, state, message.chat.id, int(when.timestamp()))


async def _set_note(message: Message, app: App, state: FSMContext, tg_id: int, note: str) -> None:
    await state.update_data(note=note)
    if (await state.get_data()).get("mode") == "edit":
        await _save_field(message, app, state, tg_id, note=note)
        return
    await state.set_state(TaskForm.confirm)
    data = await state.get_data()
    group = await app.storage.approved_group(tg_id) or ""
    user = await app.storage.get_user(tg_id)
    preview = GroupTask(
        0,
        group,
        tg_id,
        short_name(user.full_name or "") if user else "",
        data["course_id"],
        data["course_name"],
        data["title"],
        note,
        data["deadline"],
    ).as_activity()
    kb = InlineKeyboardBuilder()
    kb.button(text="Сохранить и разослать группе", callback_data="gt:save")
    kb.button(text="Отмена", callback_data="gt:x")
    kb.adjust(1)
    text = "<b>Проверь задание</b>\n\n" + activity_line(preview, int(time.time()))
    await message.answer(text, reply_markup=kb.as_markup())


async def got_note(message: Message, app: App, state: FSMContext) -> None:
    if await _menu_pressed(message, state):
        return
    note = (message.text or "").strip()
    if len(note) > NOTE_MAX:
        await message.answer(f"Слишком длинно: до {NOTE_MAX} символов (сейчас {len(note)}).")
        return
    await _set_note(message, app, state, message.chat.id, note)


async def cb_skip_note(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _set_note(callback.message, app, state, callback.from_user.id, "")


async def cb_save(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    message = callback.message
    data = await state.get_data()
    if not isinstance(message, Message) or "deadline" not in data or data.get("mode") != "new":
        await callback.answer("Черновик устарел, начни заново")
        return
    group = await _my_group(app, callback.from_user.id, message)
    user = await app.storage.get_user(callback.from_user.id)
    if group is None or user is None:
        await callback.answer()
        return
    await callback.answer("Сохраняю…")
    await state.clear()
    task_id = await app.storage.add_group_task(
        group,
        user.tg_id,
        short_name(user.full_name or ""),
        data["course_id"],
        data["course_name"],
        data["title"],
        data["note"],
        data["deadline"],
    )
    task = await app.storage.group_task(task_id)
    assert task is not None
    sent = await notify_group(app, task, NewActivity(task.as_activity()), exclude=user.tg_id)
    await message.answer(f"Сохранила задание. Уведомление получили {sent} чел. из группы.")
    await show_panel(message, app, state, user.tg_id)


async def cb_cancel(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer("Отменила")
    await state.clear()
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await show_panel(callback.message, app, state, callback.from_user.id)


async def _menu_pressed(message: Message, state: FSMContext) -> bool:
    if (message.text or "") in MENU_BUTTONS:
        await state.clear()
        await message.answer("Отменила редактирование задания. Нажми кнопку ещё раз.")
        return True
    return False


# ---- карточка задания ------------------------------------------------------------------


async def _own_task(app: App, tg_id: int, task_id: int) -> GroupTask | None:
    group = await app.storage.approved_group(tg_id)
    task = await app.storage.group_task(task_id)
    return task if task is not None and group is not None and task.group_name == group else None


async def show_task(message: Message, app: App, task: GroupTask) -> None:
    kb = InlineKeyboardBuilder()
    for label, field in (
        ("Название", "title"),
        ("Предмет", "course"),
        ("Срок", "deadline"),
        ("Пояснение", "note"),
    ):
        kb.button(text=label, callback_data=f"gt:f:{field}:{task.id}")
    kb.button(text="Удалить", callback_data=f"gt:del:{task.id}")
    kb.button(text="Назад к заданиям", callback_data="gt:panel")
    kb.adjust(2, 2, 1, 1)
    a = task.as_activity()
    text = "<b>Задание группы</b>\n\n" + activity_line(a, int(time.time()))
    await message.answer(text, reply_markup=kb.as_markup())


async def cb_edit(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    message = callback.message
    task = await _own_task(app, callback.from_user.id, int((callback.data or "gt:e:0").rsplit(":", 1)[1]))
    if not isinstance(message, Message):
        return
    if task is None:
        await message.answer("Этого задания уже нет.")
        return
    await state.clear()
    await show_task(message, app, task)


async def cb_field(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    await callback.answer()
    message = callback.message
    _, _, field, raw = (callback.data or "").split(":")
    task = await _own_task(app, callback.from_user.id, int(raw))
    if not isinstance(message, Message):
        return
    if task is None:
        await message.answer("Этого задания уже нет.")
        return
    await state.clear()
    await state.update_data(mode="edit", task_id=task.id)
    if field == "course":
        await _ask_course(message, app, state, callback.from_user.id, "")
    elif field == "title":
        await state.set_state(TaskForm.title)
        await message.answer(
            f"Сейчас: {escape(task.title)}\n\nНапиши новое название.", reply_markup=_cancel_kb()
        )
    elif field == "deadline":
        await state.set_state(TaskForm.deadline)
        await message.answer(
            f"Сейчас: {fmt_dt(task.deadline)}\n\n{DEADLINE_HINT}", reply_markup=_deadline_kb()
        )
    elif field == "note":
        await state.set_state(TaskForm.note)
        kb = InlineKeyboardBuilder()
        kb.button(text="Убрать пояснение", callback_data="gt:skip")
        kb.button(text="Отмена", callback_data="gt:x")
        current = escape(task.note) if task.note else "нет"
        await message.answer(f"Сейчас: {current}\n\nНапиши новое пояснение.", reply_markup=kb.as_markup())


async def _save_field(message: Message, app: App, state: FSMContext, tg_id: int, **fields: str | int) -> None:
    data = await state.get_data()
    await state.clear()
    task = await _own_task(app, tg_id, int(data.get("task_id", 0)))
    if task is None:
        await message.answer("Этого задания уже нет.")
        return
    await app.storage.update_group_task(task.id, **fields)
    updated = await app.storage.group_task(task.id)
    assert updated is not None
    if "deadline" in fields and fields["deadline"] != task.deadline:
        event = DeadlineChanged(updated.as_activity(), task.deadline)
        sent = await notify_group(app, updated, event, exclude=tg_id)
        await message.answer(f"Срок изменён. Уведомление получили {sent} чел. из группы.")
    else:
        await message.answer("Сохранила.")
    await show_task(message, app, updated)


async def cb_delete(callback: CallbackQuery, app: App) -> None:
    await callback.answer()
    message = callback.message
    raw = (callback.data or "").rsplit(":", 1)[1]
    task = await _own_task(app, callback.from_user.id, int(raw))
    if not isinstance(message, Message) or task is None:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"gt:del!:{task.id}")
    kb.button(text="Нет", callback_data=f"gt:e:{task.id}")
    await message.answer(
        f"Удалить задание «{escape(task.title)}» у всей группы?", reply_markup=kb.as_markup()
    )


async def cb_delete_confirm(callback: CallbackQuery, app: App, state: FSMContext) -> None:
    message = callback.message
    task = await _own_task(app, callback.from_user.id, int((callback.data or "").rsplit(":", 1)[1]))
    if task is not None:
        await app.storage.delete_group_task(task.id)
    await callback.answer("Удалила" if task else "Уже удалено")
    if isinstance(message, Message):
        await show_panel(message, app, state, callback.from_user.id)


# ---- рассылка группе -------------------------------------------------------------------


async def notify_group(app: App, task: GroupTask, event: Event, *, exclude: int) -> int:
    """Поставить сообщение в очередь каждому участнику группы (кроме автора) и отправить.
    Наступившие пороги напоминаний отмечаем сразу: человек только что увидел срок."""
    now = int(time.time())
    activity = task.as_activity()
    count = 0
    for member in await app.storage.group_members(task.group_name):
        if member.tg_id == exclude or not member.initialized:
            continue
        outgoing = render_events([event], member.settings, now)
        marks = already_due_marks(activity, member.settings.reminders, now)
        await app.storage.apply_marks(member.tg_id, marks, outgoing, now)
        count += bool(outgoing)
    await app.notifier.deliver()
    return count


def build_router() -> Router:
    """Новый роутер на каждый вызов: диспетчер можно собрать сколько угодно раз."""
    r = Router(name="starosta")
    r.message.register(cmd_request, Command("starosta"))
    r.callback_query.register(cb_request, F.data == "st:req")
    r.message.register(got_proof, ApplyForm.proof, F.photo | F.document)
    r.message.register(proof_not_file, ApplyForm.proof)
    r.callback_query.register(cb_deputy_choice, F.data.startswith("st:dep:"), ApplyForm.deputy)
    r.callback_query.register(cb_add_deputy, F.data == "gt:dep")
    r.callback_query.register(cb_pick_deputy, F.data.startswith("gt:dep:"))
    r.callback_query.register(cb_confirm_deputy, F.data.startswith("gt:depok:"))
    r.callback_query.register(cb_remove_deputy, F.data == "gt:undep")
    r.callback_query.register(cb_confirm_remove_deputy, F.data == "gt:undep!")
    r.callback_query.register(cb_decide, F.data.startswith("st:ok:") | F.data.startswith("st:no:"))
    r.message.register(cmd_list, Command("starostas"))
    r.callback_query.register(cb_remove, F.data.startswith("st:rm:"))
    # «Для старосты» — подпись кнопки до переименования, у кого-то клавиатура ещё старая
    r.message.register(show_section, F.text.in_({BTN_STAROSTA, BTN_GROUP_PANEL, "Для старосты"}))
    r.message.register(show_section, Command("group"))
    r.callback_query.register(cb_guide, F.data == "st:guide")
    r.callback_query.register(cb_panel, F.data == "gt:panel")
    r.callback_query.register(cb_roster, F.data == "gt:roster")
    r.callback_query.register(cb_new, F.data == "gt:new")
    r.callback_query.register(cb_course, F.data.startswith("gt:c:"), TaskForm.course)
    r.message.register(got_title, TaskForm.title, F.text)
    r.callback_query.register(cb_deadline, F.data.startswith("gt:d:"), TaskForm.deadline)
    r.message.register(got_deadline, TaskForm.deadline, F.text)
    r.message.register(got_note, TaskForm.note, F.text)
    r.callback_query.register(cb_skip_note, F.data == "gt:skip", TaskForm.note)
    r.callback_query.register(cb_save, F.data == "gt:save", TaskForm.confirm)
    r.callback_query.register(cb_cancel, F.data == "gt:x")
    r.callback_query.register(cb_edit, F.data.startswith("gt:e:"))
    r.callback_query.register(cb_field, F.data.startswith("gt:f:"))
    r.callback_query.register(cb_delete_confirm, F.data.startswith("gt:del!:"))
    r.callback_query.register(cb_delete, F.data.startswith("gt:del:"))
    return r
