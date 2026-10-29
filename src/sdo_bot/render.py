"""Тексты сообщений (HTML-разметка Telegram)."""

from __future__ import annotations

import time
from datetime import datetime
from html import escape

from .config import MSK
from .events import (
    DeadlineChanged,
    Event,
    GradeChanged,
    MoodleNotice,
    NewActivity,
    NewCourse,
    NewMaterials,
    QuizOpened,
    Reminder,
    Welcome,
)
from .moodle.models import Activity, CourseGrade, GradeItem, Module
from .outgoing import Outgoing
from .storage import UserSettings

LIMIT = 4000  # у Telegram 4096, оставляем запас на разметку
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MOD_ICONS = {
    "quiz": "📝",
    "assign": "📄",
    "resource": "📎",
    "folder": "🗂",
    "url": "🔗",
    "page": "📃",
    "forum": "💬",
    "lesson": "📘",
    "book": "📚",
    "workshop": "🧩",
    "choice": "🗳",
    "feedback": "📋",
    "glossary": "🔤",
    "wiki": "📝",
    "scorm": "🎓",
    "h5pactivity": "🎓",
    "bigbluebuttonbn": "🎥",
    "group": "⭐",  # задание старосты
}
STAR = "⭐"


# ---- общие куски -------------------------------------------------------------------------


def fmt_dt(ts: int) -> str:
    d = datetime.fromtimestamp(ts, MSK)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m} в {d:%H:%M}"


def fmt_span(seconds: int) -> str:
    seconds = max(0, seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} дн {hours} ч" if hours else f"{days} дн"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{max(minutes, 1)} мин"


def fmt_hours(h: int) -> str:
    if h % 24 == 0 and h >= 24:
        d = h // 24
        return "сутки" if d == 1 else f"{d} сут"
    return f"{h} ч"


def icon(modname: str) -> str:
    return MOD_ICONS.get(modname, "📌")


def link(text: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(text)}</a>' if url else escape(text)


def kind_name(a: Activity) -> str:
    return {"quiz": "тест", "assign": "задание", "group": "задание старосты"}.get(a.modname, "элемент курса")


def short_name(full_name: str) -> str:
    """«Фамилия Имя» из ФИО."""
    return " ".join(full_name.split()[:2])


def starosta_footer(acts: list[Activity]) -> list[str]:
    """Сноска к спискам со звёздочками: кто из старост поставил задания."""
    authors: dict[str, set[str]] = {}
    for a in acts:
        if a.by_starosta:
            authors.setdefault(a.group, set()).add(a.author)
    lines = []
    for group, names in sorted(authors.items()):
        who = ", ".join(sorted(names))
        lines.append(f"{STAR} — задание от старосты группы {escape(group)}: {escape(who)}")
    return lines


def new_word(a: Activity) -> str:
    return "Новое" if a.by_starosta else "Новый"


def open_button(a: Activity, label: str = "Открыть") -> tuple[str, str] | None:
    return (label, a.url) if a.url else None


def activity_line(a: Activity, now: int) -> str:
    head = f"{icon(a.modname)} <b>{link(a.name, a.url)}</b>"
    if a.course_name:
        head += f"\n    <i>{escape(a.course_name)}</i>"
    head += "\n    " + activity_timing(a, now)
    if a.by_starosta:
        if a.note:
            head += f"\n    {escape(a.note)}"
        head += f"\n    <i>От старосты группы {escape(a.group)}: {escape(a.author)}</i>"
    return head


def activity_timing(a: Activity, now: int) -> str:
    if a.open_ts and a.open_ts > now:
        s = f"откроется {fmt_dt(a.open_ts)} (через {fmt_span(a.open_ts - now)})"
        if a.close_ts:
            s += f", закроется {fmt_dt(a.close_ts)}"
        return s
    if a.close_ts is None:
        return "без срока"
    if a.close_ts <= now:
        return f"срок был {fmt_dt(a.close_ts)}"
    return f"до {fmt_dt(a.close_ts)} · осталось {fmt_span(a.close_ts - now)}"


def chunk(lines: list[str], sep: str = "\n") -> list[str]:
    """Разбить длинный текст на сообщения по границам строк."""
    parts: list[str] = []
    cur = ""
    for line in lines:
        candidate = f"{cur}{sep}{line}" if cur else line
        if len(candidate) > LIMIT and cur:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


# ---- списки по запросу ---------------------------------------------------------------------


def deadline_status(a: Activity, now: int) -> str:
    """Срок одной строкой для списков по предметам."""
    if a.close_ts is not None and a.close_ts <= now:
        if a.by_starosta:
            return f"срок прошёл {fmt_dt(a.close_ts)}"
        return f"срок был {fmt_dt(a.close_ts)}, ещё можно сдать"
    return activity_timing(a, now)


def deadline_order(a: Activity, now: int) -> tuple[int, int]:
    """Внутри предмета: сначала идущие (по сроку), потом будущие, в конце просроченные."""
    if a.close_ts is not None and a.close_ts <= now:
        return 2, a.close_ts
    if a.open_ts is not None and a.open_ts > now:
        return 1, a.open_ts
    return 0, a.close_ts or 2**62


def group_by_course(acts: list[Activity]) -> list[tuple[str, list[Activity]]]:
    """Работы по дисциплинам, дисциплины по алфавиту."""
    groups: dict[int, list[Activity]] = {}
    for a in acts:
        groups.setdefault(a.course_id, []).append(a)
    named = [(g[0].course_name or f"Курс {cid}", g) for cid, g in groups.items()]
    return sorted(named, key=lambda x: x[0].casefold())


def course_deadlines_lines(name: str, acts: list[Activity], now: int) -> list[str]:
    lines = [f"<b>{escape(name)}</b>"]
    for a in sorted(acts, key=lambda a: deadline_order(a, now)):
        if a.by_starosta:
            line = f"{STAR} Задание: {escape(a.name)}\n    {deadline_status(a, now)}"
            if a.note:
                line += f"\n    <i>{escape(a.note)}</i>"
        else:
            line = f"{kind_name(a).capitalize()}: {link(a.name, a.url)}\n    {deadline_status(a, now)}"
        lines.append(line)
    return lines


def all_deadlines_view(acts: list[Activity], now: int, synced_at: int | None) -> list[str]:
    if not acts:
        lines = ["Нет открытых тестов и заданий со сроком."]
    else:
        lines = [f"<b>Все дедлайны</b> ({len(acts)})"]
        for name, group in group_by_course(acts):
            lines.append("")
            lines += course_deadlines_lines(name, group, now)
        if footer := starosta_footer(acts):
            lines += ["", *footer]
    if synced_at:
        lines.append(f"\n<i>Данные СДО на {datetime.fromtimestamp(synced_at, MSK):%H:%M}</i>")
    return chunk(lines)


def course_deadlines_view(name: str, acts: list[Activity], now: int, synced_at: int | None) -> list[str]:
    lines = course_deadlines_lines(name, acts, now) if acts else [f"<b>{escape(name)}</b>", "Сроков нет."]
    if footer := starosta_footer(acts):
        lines += ["", *footer]
    if synced_at:
        lines.append(f"\n<i>Данные СДО на {datetime.fromtimestamp(synced_at, MSK):%H:%M}</i>")
    return chunk(lines)


def grades_overview_view(grades: list[CourseGrade]) -> str:
    if not grades:
        return "📊 В отчёте об оценках пока нет курсов."
    lines = ["📊 <b>Оценки по дисциплинам</b>", ""]
    for g in grades:
        value = g.grade if g.grade not in ("", "-") else "—"
        lines.append(f"• {escape(g.course_name)} — <b>{escape(value)}</b>")
    lines.append("\nНажми на дисциплину, чтобы увидеть оценки за отдельные работы.")
    return "\n".join(lines)


def course_grades_view(course_name: str, items: list[GradeItem], url: str) -> list[str]:
    lines = [f"📊 <b>{link(course_name, url)}</b>", ""]
    graded = [i for i in items if not i.is_total]
    for it in graded:
        value = it.grade if it.has_grade else "—"
        extra = f" из {escape(it.range)}" if it.range and it.has_grade else ""
        lines.append(f"• {escape(it.name)}: <b>{escape(value)}</b>{extra}")
    total = next((i for i in items if i.is_total), None)
    if total is not None:
        lines.append(f"\n<b>Итог курса: {escape(total.grade if total.has_grade else '—')}</b>")
        if total.range and total.has_grade:
            lines[-1] += f" из {escape(total.range)}"
    if not graded and total is None:
        lines.append("Оценок пока нет.")
    return chunk(lines)


# ---- уведомления ---------------------------------------------------------------------------


def render_events(events: list[Event], settings: UserSettings, now: int | None = None) -> list[Outgoing]:
    now = now or int(time.time())
    out: list[Outgoing] = []
    materials: list[NewMaterials | NewCourse] = []
    grades: list[GradeChanged] = []
    notices: list[MoodleNotice] = []

    for e in events:
        if isinstance(e, Welcome):
            out += [Outgoing(t) for t in welcome_text(e, settings, now)]
        elif isinstance(e, NewActivity) and settings.new_items:
            a = e.activity
            out.append(
                Outgoing(f"🆕 <b>{new_word(a)} {kind_name(a)}</b>\n\n{activity_line(a, now)}", open_button(a))
            )
        elif isinstance(e, DeadlineChanged) and settings.deadline_changes:
            out.append(Outgoing(deadline_changed_text(e, now), open_button(e.activity)))
        elif isinstance(e, QuizOpened) and settings.openings:
            a = e.activity
            title = "Тест открыт!" if a.is_quiz else "Доступ открыт!"
            out.append(
                Outgoing(
                    f"🔓 <b>{title}</b>\n\n{activity_line(a, now)}", ("Начать", a.url), expires_at=a.close_ts
                )
            )
        elif isinstance(e, Reminder):
            a = e.activity
            left = fmt_span((a.close_ts or now) - now)
            out.append(
                Outgoing(
                    f"⏰ <b>До срока осталось {left}</b>\n\n{activity_line(a, now)}",
                    open_button(a),
                    expires_at=a.close_ts,
                )
            )
        elif isinstance(e, NewMaterials | NewCourse) and settings.materials:
            materials.append(e)
        elif isinstance(e, GradeChanged) and settings.grades:
            grades.append(e)
        elif isinstance(e, MoodleNotice) and settings.moodle_notifications:
            notices.append(e)

    if materials:
        out += [Outgoing(t) for t in materials_text(materials)]
    if grades:
        out += [Outgoing(t) for t in grades_text(grades)]
    for n in notices[:5]:
        nt = n.notification
        body = f"🔔 <b>{escape(nt.subject or 'Уведомление СДО')}</b>"
        if nt.text and nt.text != nt.subject:
            body += f"\n\n{escape(nt.text)}"
        out.append(Outgoing(body, ("Открыть в СДО", nt.url) if nt.url else None))
    if len(notices) > 5:
        out.append(Outgoing(f"🔔 И ещё {len(notices) - 5} уведомлений в СДО."))
    return out


def first_name(full_name: str) -> str:
    """В СДО МИРЭА полное имя записано как «Фамилия Имя Отчество»."""
    parts = full_name.split()
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "")


def welcome_text(w: Welcome, settings: UserSettings, now: int) -> list[str]:
    name = first_name(w.full_name)
    greeting = f"Готово, {escape(name)}!" if name else "Готово!"
    lines = [f"✅ <b>{greeting}</b> Я подключилась к СДО и слежу за ней.", ""]
    if w.active:
        lines.append(f"🔥 <b>Уже идут</b> ({len(w.active)})")
        lines += [activity_line(a, now) for a in w.active]
    if w.upcoming:
        lines.append(("\n" if w.active else "") + f"⏳ <b>Скоро откроются</b> ({len(w.upcoming)})")
        lines += [activity_line(a, now) for a in w.upcoming]
    if w.overdue:
        lines.append(f"\n⚠️ <b>Срок прошёл, но ещё можно сдать</b> ({len(w.overdue)})")
        lines += [activity_line(a, now) for a in w.overdue]
    if not (w.active or w.upcoming or w.overdue):
        lines.append("🎉 Сейчас нет открытых тестов и заданий со сроком.")
    reminders = " и ".join(fmt_hours(h) for h in sorted(settings.reminders, reverse=True))
    lines.append("")
    lines.append(f"Курсов в работе: {w.courses}.")
    if reminders:
        lines.append(f"Напомню о сроках за {reminders}. Изменить — в ⚙️ Настройках.")
    return chunk(lines)


def deadline_changed_text(e: DeadlineChanged, now: int) -> str:
    a = e.activity
    lines = [f"🔄 <b>Срок изменён</b>\n\n{icon(a.modname)} <b>{link(a.name, a.url)}</b>"]
    if a.course_name:
        lines.append(f"<i>{escape(a.course_name)}</i>")
    lines.append("")
    if e.old_close_ts:
        lines.append(f"Было: {fmt_dt(e.old_close_ts)}")
    if a.close_ts:
        diff = a.close_ts - (e.old_close_ts or a.close_ts)
        note = ""
        if diff > 0:
            note = f" (продлили на {fmt_span(diff)})"
        elif diff < 0:
            note = f" (сдвинули раньше на {fmt_span(-diff)})"
        lines.append(f"Стало: <b>{fmt_dt(a.close_ts)}</b>{note}")
        if a.close_ts > now:
            lines.append(f"Осталось: {fmt_span(a.close_ts - now)}")
    return "\n".join(lines)


def materials_text(items: list[NewMaterials | NewCourse]) -> list[str]:
    lines = []
    for e in items:
        if isinstance(e, NewCourse):
            lines.append(f"📚 <b>Новый курс:</b> {link(e.course.name, e.course.url)}")
            continue
        lines.append(f"📎 <b>Новое в курсе {link(e.course.name, e.course.url)}</b>")
        lines += [_module_line(m) for m in e.modules[:15]]
        if len(e.modules) > 15:
            lines.append(f"…и ещё {len(e.modules) - 15}")
        lines.append("")
    return chunk(lines)


def _module_line(m: Module) -> str:
    kind = f" <i>({escape(m.modplural.lower())})</i>" if m.modname not in ("resource", "url") else ""
    return f"{icon(m.modname)} {link(m.name, m.url)}{kind}"


def grades_text(items: list[GradeChanged]) -> list[str]:
    lines = ["📊 <b>Новые оценки</b>"]
    current_course = None
    for g in items:
        if g.course_id != current_course:
            current_course = g.course_id
            lines.append(f"\n<i>{escape(g.course_name)}</i>")
        if g.item_name is None:
            was = f" (было {escape(g.old)})" if g.old and g.old not in ("-", "") else ""
            lines.append(f"• Итог курса: <b>{escape(g.new)}</b>{was}")
            continue
        rng = f" из {escape(g.range)}" if g.range else ""
        was = f" (было {escape(g.old)})" if g.old and g.old not in ("-", "") else ""
        lines.append(f"• {escape(g.item_name)}: <b>{escape(g.new)}</b>{rng}{was}")
    totals = {g.course_id: g.total for g in items if g.item_name and g.total}
    if totals:
        lines.append("")
        for cid, total in totals.items():
            name = next(g.course_name for g in items if g.course_id == cid)
            lines.append(f"Итог по «{escape(name)}»: <b>{escape(total or '')}</b>")
    return chunk(lines)
