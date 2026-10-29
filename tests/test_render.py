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
