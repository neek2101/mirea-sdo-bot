from __future__ import annotations

from sdo_bot.events import DeadlineChanged, GradeChanged, NewActivity, NewMaterials, Reminder, Welcome
from sdo_bot.moodle.models import Activity, Course, Module
from sdo_bot.render import LIMIT, chunk, fmt_span, render_events
from sdo_bot.storage import UserSettings

from .conftest import NOW, H

QUIZ = Activity(
    "quiz:1", "quiz", "КР <1>", 10, "Матан & Ко", "https://x/mod/quiz/view.php?id=1", NOW - H, NOW + 2 * H
)


def test_fmt_span():
    assert fmt_span(30) == "1 мин"
    assert fmt_span(2 * H + 15 * 60) == "2 ч 15 мин"
    assert fmt_span(26 * H) == "1 дн 2 ч"


def test_welcome_lists_running_tests_and_escapes_html():
    [text] = [
        o.text
        for o in render_events(
            [Welcome(active=[QUIZ], upcoming=[], overdue=[], courses=3, full_name="Иванов Иван Иванович")],
            UserSettings(),
            now=NOW,
        )
    ]
    assert "Готово, Иван!" in text
    assert "Уже идут" in text and "КР &lt;1&gt;" in text and "Матан &amp; Ко" in text
    assert "осталось 2 ч" in text
    assert "за сутки и 3 ч" in text


def test_settings_filter_categories_but_reminders_always_go():
    s = UserSettings(new_items=False, materials=False)
    out = render_events(
        [
            NewActivity(QUIZ),
            NewMaterials(Course(10, "Матан", "u"), [Module(1, 10, "Лекция", "resource", "Файлы", "u", "S")]),
            Reminder(QUIZ, 3),
        ],
        s,
        now=NOW,
    )
    assert len(out) == 1 and "До срока осталось" in out[0].text
    assert out[0].button == ("Открыть", QUIZ.url)


def test_deadline_change_text():
    a = Activity("assign:2", "assign", "ДЗ", 1, "Курс", "u", None, NOW + 50 * H)
    [o] = render_events([DeadlineChanged(a, NOW + 26 * H)], UserSettings(), now=NOW)
    assert "Срок изменён" in o.text and "продлили на 1 дн" in o.text


def test_grades_grouped_in_one_message():
    out = render_events(
        [
            GradeChanged(10, "Матан", "Тест 1", None, "8,00", "0–10", "20,00"),
            GradeChanged(10, "Матан", "Тест 2", "-", "9,00", "0–10", "20,00"),
            GradeChanged(20, "Физика", None, "10,00", "15,00"),
        ],
        UserSettings(),
        now=NOW,
    )
    assert len(out) == 1
    assert (
        "Тест 2: <b>9,00</b> из 0–10" in out[0].text
        and "Итог курса: <b>15,00</b> (было 10,00)" in out[0].text
    )


def test_chunk_respects_telegram_limit():
    lines = [str(i) * 1000 for i in range(10)]
    parts = chunk(lines)
    assert all(len(p) <= LIMIT for p in parts)
    assert "\n".join(parts) == "\n".join(lines)  # ничего не потеряно и не разрезано посреди строки


def test_empty_env_values_mean_unset(monkeypatch, tmp_path):
    from sdo_bot.config import Settings

    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1:x\nSECRET_KEY=k\nMIREA_PROXY=\nTELEGRAM_PROXY=\n")
    monkeypatch.chdir(tmp_path)
    for name in ("BOT_TOKEN", "SECRET_KEY", "MIREA_PROXY", "TELEGRAM_PROXY"):
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.mirea_proxy is None and s.telegram_proxy is None


def test_course_names_lose_part_semester_and_underscores():
    from sdo_bot.moodle.models import clean_course_name

    cases = {
        "Программирование_Зачет (часть 1/2) [I.26-27]": "Программирование — Зачет",
        "Философия_Экзамен 2 курс (часть 1/1) [I.26-27]": "Философия — Экзамен 2 курс",
        "Ознакомительная практика (часть 1/2) [I.26-27] УИБО": "Ознакомительная практика УИБО",
        "Иностранный язык БРС (часть 2) [II.25-26]": "Иностранный язык БРС",
        "Системы ИИ ИТУ (управление) (часть 1/2) [I.26-27]": "Системы ИИ ИТУ управление",
        "Как учиться в электронной среде": "Как учиться в электронной среде",
    }
    for raw, clean in cases.items():
        assert clean_course_name(raw) == clean
        assert clean_course_name(clean) == clean  # повторная чистка ничего не портит


def test_max_points_and_wording():
    from sdo_bot.moodle.models import max_points
    from sdo_bot.render import points_text

    assert [max_points(r) for r in ("0–10", "0,00–2,50", "0-100", "Шкала", "")] == [
        "10",
        "2,5",
        "100",
        "",
        "",
    ]
    assert [points_text(v) for v in ("1", "3", "10", "11", "21", "2,5")] == [
        "1 балл",
        "3 балла",
        "10 баллов",
        "11 баллов",
        "21 балл",
        "2,5 балла",
    ]


def test_points_shown_in_deadlines_and_notifications():
    from dataclasses import replace

    from sdo_bot.render import all_deadlines_view

    quiz = replace(QUIZ, max_grade="10")
    [text] = all_deadlines_view([quiz], NOW, None)
    assert "КР &lt;1&gt;</a> · 10 баллов" in text
    [msg] = render_events([NewActivity(quiz)], UserSettings(), now=NOW)
    assert "· 10 баллов" in msg.text
    [text] = all_deadlines_view([QUIZ], NOW, None)  # максимум не известен — без хвоста
    assert "балл" not in text


def test_locked_work_shown_as_closed_not_running():
    from dataclasses import replace

    from sdo_bot.render import all_deadlines_view

    locked = replace(QUIZ, open_ts=None, available=False)
    [text] = all_deadlines_view([locked], NOW, None)
    assert "тест пока закрыт · срок до" in text
    assert "осталось" not in text
