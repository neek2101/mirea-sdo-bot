"""Старосты: заявка, одобрение, задания группы, рассылка, звёздочки, удаление после срока."""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from aiogram.methods import SendMessage, SendPhoto

from sdo_bot.bot.common import BTN_DEADLINES, BTN_GROUP_PANEL, BTN_STAROSTA
from sdo_bot.bot.starosta import parse_deadline
from sdo_bot.config import MSK
from sdo_bot.storage import GROUP_TASK_TTL

from .test_bot import TG, env  # noqa: F401 — фикстура сквозного теста бота

pytestmark = pytest.mark.anyio
GROUP = "УИБО-09-25"


def sent_to(requests, chat_id: int) -> list[str]:
    return [m.text for m in requests if isinstance(m, SendMessage) and m.chat_id == chat_id]


def menu_of(requests, chat_id: int) -> list[str]:
    """Кнопки последней клавиатуры меню, которую бот прислал этому человеку."""
    sent = [m for m in requests if isinstance(m, SendMessage) and m.chat_id == chat_id and m.reply_markup]
    keyboard = getattr(sent[-1].reply_markup, "keyboard", None) if sent else None
    return [b.text for row in keyboard or [] for b in row]


async def _classmate(storage, vault, session_cookies) -> int:
    """Второй студент той же группы, уже прошедший первичную синхронизацию."""
    tg = 555
    await storage.ensure_user(tg)
    await storage.set_session(tg, vault.seal(session_cookies), 8, "Петров Пётр Петрович")
    await storage.set_group(tg, GROUP, int(time.time()))
    await storage.db.execute("UPDATE users SET initialized = 1 WHERE tg_id = ?", (tg,))
    await storage.db.commit()
    return tg


async def test_starosta_flow(env, storage, vault, session_cookies):  # noqa: F811
    session, say, press, app = env
    app.settings.admin_ids = frozenset({TG})

    await say("/cookie good-session")
    assert (await storage.get_user(TG)).group_name == GROUP  # группа определилась при синхронизации
    session.take()

    # Заявка: пояснение про ручное согласование → доказательство → вопрос про заместителя
    await press("st:req")
    intro = session.texts()[-1]
    assert "администратор бота вручную" in intro and "один студент" in intro and "фото" in intro
    await say("я правда староста")
    assert "Нужно фото или файл" in session.texts()[-1]
    await say(photo="proof-photo")
    assert "Есть ли у тебя заместитель?" in session.texts()[-1]
    session.take()
    await press("st:dep:1")

    # Администратору приходит само доказательство с подписью и кнопками
    to_admin = [m for m in session.requests if isinstance(m, SendPhoto)]
    assert len(to_admin) == 1 and to_admin[0].photo == "proof-photo"
    assert GROUP in to_admin[0].caption and "Заместитель: будет" in to_admin[0].caption
    assert [b.callback_data for b in to_admin[0].reply_markup.inline_keyboard[0]] == [
        f"st:ok:{TG}",
        f"st:no:{TG}",
    ]
    assert "рассматривает один студент" in session.texts()[-1]
    session.take()

    # Одобрение: у старосты «Панель группы», одногруппнику — новость и меню без «Для старост»
    mate = await _classmate(storage, vault, session_cookies)
    await press(f"st:ok:{TG}")
    sent = session.take()
    assert any("одобрили" in t and "Добавить заместителя" in t for t in sent_to(sent, TG))
    assert BTN_GROUP_PANEL in menu_of(sent, TG) and BTN_STAROSTA not in menu_of(sent, TG)
    assert any("появился староста: Иванов Иван" in t for t in sent_to(sent, mate))
    assert BTN_STAROSTA not in menu_of(sent, mate) and BTN_GROUP_PANEL not in menu_of(sent, mate)

    # Мастер: предмет → название → срок → пояснение → сохранить
    deadline = int(time.time()) + 3 * 86400
    await say(BTN_GROUP_PANEL)
    await press("gt:new")
    assert "шаг 1 из 4" in session.texts()[-1]
    await press("gt:c:1")
    await say("Лабораторная по оптике")
    await press(f"gt:d:{deadline}")
    await say("Отчёт в тетради, без титульника")
    assert "Проверь задание" in session.texts()[-1]
    await press("gt:save")
    assert any("Уведомление получили 1 чел." in t for t in session.texts())
    new = sent_to(session.take(), mate)
    assert any("Новое задание старосты" in t and "⭐" in t and "Иванов Иван" in t for t in new), new

    # В списке дедлайнов у одногруппника: звёздочка, предмет жирным, сноска со старостой
    acts = await app.storage.group_tasks(GROUP, int(time.time()))
    assert [(t.title, t.course_name, t.author_name) for t in acts] == [
        ("Лабораторная по оптике", "Физика", "Иванов Иван")
    ]
    await say(BTN_DEADLINES)
    text = "\n".join(session.texts())
    session.take()
    assert "⭐ Задание: Лабораторная по оптике" in text
    assert f"⭐ — задание от старосты группы {GROUP}: Иванов Иван" in text

    # Перенос срока → одногруппнику «Срок изменён»
    task_id = acts[0].id
    later = deadline + 86400
    await press(f"gt:e:{task_id}")
    await press(f"gt:f:deadline:{task_id}")
    await press(f"gt:d:{later}")
    assert any("Срок изменён. Уведомление получили 1 чел." in t for t in session.texts())
    assert any("Срок изменён" in t for t in sent_to(session.take(), mate))

    # Напоминание за сутки приходит и одногруппнику, и самому старосте
    reminders = await app.sync.due_reminders(now=later - 20 * 3600)
    assert mate in reminders and any(e.activity.key == f"group:{task_id}" for e in reminders[mate])

    # Через сутки после срока задание исчезает само
    assert await storage.purge_group_tasks(later + GROUP_TASK_TTL - 1) == 0
    assert await storage.purge_group_tasks(later + GROUP_TASK_TTL) == 1
    assert await storage.group_tasks(GROUP, later) == []


async def test_roster_marks_bot_users(env, storage, vault, session_cookies):  # noqa: F811
    session, say, press, _app = env
    await say("/cookie good-session")
    await storage.set_starosta(TG, GROUP, "approved", int(time.time()))
    await _classmate(storage, vault, session_cookies)
    session.take()
    await press("gt:roster")
    text = session.texts()[-1]
    assert "Пользуются ботом: 2" in text
    assert "1. Иванов Иван Иванович — в боте" in text and "2. Петров Пётр Петрович — в боте" in text


async def test_section_explains_role_to_non_starosta(env, storage):  # noqa: F811
    session, say, press, app = env
    app.settings.admin_ids = frozenset({1})
    await say("/cookie good-session")
    session.take()

    # Кнопка видна всем; не старосте — описание раздела и «Подать заявку»
    await say(BTN_STAROSTA)
    guide = next(m for m in session.take() if isinstance(m, SendMessage))
    assert "Что может староста" in guide.text and "Что видят одногруппники" in guide.text
    assert "Как стать старостой" in guide.text and "один студент" in guide.text
    assert guide.reply_markup.inline_keyboard[0][0].callback_data == "st:req"
    await press("gt:new")  # чужие действия старосты недоступны
    assert "только старосте группы" in session.texts()[-1]

    # Заявка подана: вместо кнопки — статус; одобрить её может только администратор
    await storage.set_starosta(TG, GROUP, "pending", int(time.time()))
    session.take()
    await say(BTN_STAROSTA)
    pending = next(m for m in session.take() if isinstance(m, SendMessage))
    assert "на рассмотрении" in pending.text and pending.reply_markup is None
    await press(f"st:ok:{TG}")
    assert (await storage.starosta(TG)).status == "pending"


async def test_panel_has_guide(env, storage):  # noqa: F811
    session, say, press, _app = env
    await say("/cookie good-session")
    await storage.set_starosta(TG, GROUP, "approved", int(time.time()))
    session.take()
    await say("Для старосты")  # подпись кнопки со старой клавиатуры тоже работает
    panel = next(m for m in session.take() if isinstance(m, SendMessage))
    assert "Задания группы" in panel.text and "Ты староста" in panel.text
    assert "st:guide" in [b.callback_data for row in panel.reply_markup.inline_keyboard for b in row]
    await press("st:guide")
    assert "Что может староста" in session.texts()[-1]


def test_parse_deadline():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=MSK)
    assert parse_deadline("31.10", now) == datetime(2026, 10, 31, 23, 59, tzinfo=MSK)
    assert parse_deadline("31.10 18:00", now) == datetime(2026, 10, 31, 18, 0, tzinfo=MSK)
    assert parse_deadline("5.1", now) == datetime(2027, 1, 5, 23, 59, tzinfo=MSK)  # без года — ближайшая
    assert parse_deadline("01.02.27 9.30", now) == datetime(2027, 2, 1, 9, 30, tzinfo=MSK)
    assert parse_deadline("31.02", now) is None
    assert parse_deadline("завтра", now) is None


async def test_starosta_appoints_and_removes_deputy(env, storage, vault, session_cookies):  # noqa: F811
    session, say, press, app = env
    app.settings.admin_ids = frozenset({1})
    await say("/cookie good-session")
    mate = await _classmate(storage, vault, session_cookies)
    await storage.set_starosta(TG, GROUP, "approved", int(time.time()))
    session.take()

    # Нет заместителя → в панели «Добавить заместителя», выбор из одногруппников в боте
    await say(BTN_GROUP_PANEL)
    panel = next(m for m in session.take() if isinstance(m, SendMessage))
    assert "Панель группы" in panel.text and "Заместитель: нет" in panel.text
    assert "gt:dep" in [b.callback_data for row in panel.reply_markup.inline_keyboard for b in row]
    await press("gt:dep")
    picker = next(m for m in session.take() if isinstance(m, SendMessage))
    assert picker.reply_markup.inline_keyboard[0][0].callback_data == f"gt:dep:{mate}"
    await press(f"gt:dep:{mate}")
    await press(f"gt:depok:{mate}")
    sent = session.take()
    assert (await storage.starosta(mate)).role == "deputy"
    assert any("назначил тебя заместителем" in t for t in sent_to(sent, mate))
    assert BTN_GROUP_PANEL in menu_of(sent, mate)
    assert any("назначил заместителя" in t for t in sent_to(sent, 1))  # администратор в курсе
    assert any("Заместитель: Петров Пётр" in t for t in sent_to(sent, TG))

    # Заместитель есть → «Снять заместителя»
    await press("gt:undep")
    await press("gt:undep!")
    sent = session.take()
    assert await storage.starosta(mate) is None
    assert any("снял тебя с роли заместителя" in t for t in sent_to(sent, mate))
    assert BTN_GROUP_PANEL not in menu_of(sent, mate)


async def test_deputy_cannot_manage_deputy(env, storage):  # noqa: F811
    session, say, press, _app = env
    await say("/cookie good-session")
    await storage.set_starosta(TG, GROUP, "approved", int(time.time()), role="deputy")
    session.take()
    await say(BTN_GROUP_PANEL)
    panel = next(m for m in session.take() if isinstance(m, SendMessage) and "Панель группы" in m.text)
    buttons = [b.callback_data for row in panel.reply_markup.inline_keyboard for b in row]
    assert "gt:dep" not in buttons and "gt:undep" not in buttons
    await press(f"gt:depok:{TG}")
    assert (await storage.starosta(TG)).role == "deputy"


async def test_button_hidden_once_group_has_starosta(env, storage):  # noqa: F811
    session, say, _press, _app = env
    await say("/cookie good-session")
    await storage.set_starosta(555, GROUP, "approved", int(time.time()))
    await storage.ensure_user(555)
    await storage.db.execute("UPDATE users SET full_name = 'Петров Пётр Петрович' WHERE tg_id = 555")
    await storage.db.commit()
    session.take()
    # Старая клавиатура ещё показывает кнопку — нажатие объясняет и присылает меню без неё
    await say(BTN_STAROSTA)
    sent = session.take()
    assert any("староста — Петров Пётр" in t for t in sent_to(sent, TG))
    assert BTN_STAROSTA not in menu_of(sent, TG) and BTN_GROUP_PANEL not in menu_of(sent, TG)
