from __future__ import annotations

import pytest

from sdo_bot.events import (
    DeadlineChanged,
    GradeChanged,
    MoodleNotice,
    NewActivity,
    NewCourse,
    NewMaterials,
    QuizOpened,
    Reminder,
    Welcome,
)
from sdo_bot.sync import NeedLogin

from .conftest import NOW, H
from .fakes import MOODLE, event, user_report_html

pytestmark = pytest.mark.anyio


def kinds(events):
    return [type(e).__name__ for e in events]


async def test_initial_sync_sends_only_welcome_with_running_and_upcoming_tests(sync, logged_in, storage):
    result = await sync.sync_user(logged_in, now=NOW)
    assert result.initial
    assert kinds(result.events) == ["Welcome"]
    w: Welcome = result.events[0]
    assert [a.name for a in w.active] == ["Контрольная №1", "Лабораторная 1"]
    assert [a.name for a in w.upcoming] == ["Тест по механике"]
    assert w.courses == 2
    user = await storage.get_user(logged_in)
    assert user.initialized and user.last_notification_id == 5


async def test_after_welcome_no_duplicate_open_or_reminder_for_already_running_test(sync, logged_in):
    await sync.sync_user(logged_in, now=NOW)
    # КР №1 уже идёт и закроется через 2 ч: о ней только что рассказали в приветствии
    due = await sync.due_reminders(now=NOW + 60)
    assert due == {}


async def test_changes_are_detected_on_next_sync(sync, logged_in, moodle):
    await sync.sync_user(logged_in, now=NOW)

    # Преподаватель продлил тест по механике, добавил новый тест, файл и выставил оценку
    moodle.events = [e for e in moodle.events if e["instance"] != 102] + [
        event(3, "quiz", 102, "open", NOW + 24 * H, "Тест по механике", (20, "Физика"), 2001),
        event(4, "quiz", 102, "close", NOW + 50 * H, "Тест по механике", (20, "Физика"), 2001),
        event(6, "quiz", 103, "close", NOW + 10 * H, "Тест 2", (10, "Математический анализ"), 1005),
    ]
    moodle.contents[10][0]["modules"] += [
        {
            "id": 1005,
            "name": "Тест 2",
            "modname": "quiz",
            "modplural": "Тесты",
            "url": f"{MOODLE}/mod/quiz/view.php?id=1005",
            "uservisible": True,
        },
        {
            "id": 1006,
            "name": "Лекция 2.pdf",
            "modname": "resource",
            "modplural": "Файлы",
            "url": f"{MOODLE}/mod/resource/view.php?id=1006",
            "uservisible": True,
        },
    ]
    moodle.overview = [(10, "Математический анализ", "20,00"), (20, "Физика", "-")]
    moodle.reports[10] = user_report_html(
        [(1001, "Тест", "Контрольная №0", "8,00"), (1004, "Задание", "ДЗ 1", "8,00")], "20,00"
    )
    moodle.notifications.insert(
        0,
        {
            "id": 6,
            "subject": "Ваша работа оценена",
            "smallmessage": "ДЗ 1",
            "contexturl": f"{MOODLE}/mod/assign/view.php?id=1004",
            "timecreated": NOW,
            "component": "mod_assign",
        },
    )

    result = await sync.sync_user(logged_in, now=NOW + 600)
    by_kind = {type(e).__name__: e for e in result.events}
    assert set(by_kind) == {"DeadlineChanged", "NewActivity", "NewMaterials", "GradeChanged", "MoodleNotice"}

    changed: DeadlineChanged = by_kind["DeadlineChanged"]
    assert changed.old_close_ts == NOW + 26 * H and changed.activity.close_ts == NOW + 50 * H
    assert by_kind["NewActivity"].activity.name == "Тест 2"
    # Новый тест уже объявлен — в «новых материалах» только файл
    assert [m.name for m in by_kind["NewMaterials"].modules] == ["Лекция 2.pdf"]
    grade: GradeChanged = by_kind["GradeChanged"]
    assert (grade.item_name, grade.old, grade.new, grade.total) == ("ДЗ 1", "-", "8,00", "20,00")
    assert by_kind["MoodleNotice"].notification.id == 6

    # Повторный опрос без изменений — тишина
    again = await sync.sync_user(logged_in, now=NOW + 1200)
    assert again.events == []


async def test_submitted_activity_marked_done_and_not_reminded(sync, logged_in, moodle, storage):
    await sync.sync_user(logged_in, now=NOW)
    moodle.events = [e for e in moodle.events if e["instance"] != 201]  # лабораторную сдали
    await sync.sync_user(logged_in, now=NOW + 600)
    stored = await storage.activities(logged_in)
    assert stored["assign:201"].status == "done"
    due = await sync.due_reminders(now=NOW + 71 * H)
    assert not any(isinstance(e, Reminder) and e.activity.key == "assign:201" for e in due.get(logged_in, []))


async def test_failed_fetch_is_never_treated_as_everything_disappeared(sync, logged_in, moodle, storage):
    await sync.sync_user(logged_in, now=NOW)
    moodle.fail_methods = {"core_calendar_get_action_events_by_timesort", "core_course_get_contents"}
    moodle.fail_pages = {"/grade/report/overview/index.php"}
    result = await sync.sync_user(logged_in, now=NOW + 600)
    assert result.events == [] and result.errors
    stored = await storage.activities(logged_in)
    assert all(s.status == "pending" for s in stored.values())

    # Когда СДО снова отвечает — без ложных «новых» событий
    moodle.fail_methods, moodle.fail_pages = set(), set()
    assert (await sync.sync_user(logged_in, now=NOW + 1200)).events == []


async def test_first_sync_with_partial_failure_does_not_flood_later(sync, logged_in, moodle):
    moodle.fail_methods = {"core_course_get_contents"}
    moodle.fail_pages = {"/grade/report/overview/index.php"}
    moodle.notifications = []
    moodle.fail_methods.add("message_popup_get_popup_notifications")
    first = await sync.sync_user(logged_in, now=NOW)
    assert kinds(first.events) == ["Welcome"]

    moodle.fail_methods, moodle.fail_pages = set(), set()
    moodle.notifications = [
        {
            "id": 1,
            "subject": "старое",
            "smallmessage": "",
            "contexturl": "",
            "timecreated": NOW - H,
            "component": "x",
        }
    ]
    second = await sync.sync_user(logged_in, now=NOW + 600)
    # Курсы, материалы, оценки и старые уведомления впервые зафиксированы — это не новости
    assert not any(
        isinstance(e, NewCourse | NewMaterials | GradeChanged | MoodleNotice) for e in second.events
    )


async def test_new_course_announced(sync, logged_in, moodle):
    await sync.sync_user(logged_in, now=NOW)
    moodle.courses.append({"id": 30, "fullname": "Философия", "viewurl": f"{MOODLE}/course/view.php?id=30"})
    result = await sync.sync_user(logged_in, now=NOW + 600)
    assert [e.course.name for e in result.events if isinstance(e, NewCourse)] == ["Философия"]


async def test_expired_moodle_session_is_renewed_through_sso(sync, logged_in, moodle, storage, vault):
    await sync.sync_user(logged_in, now=NOW)
    moodle.kill_sessions()
    result = await sync.sync_user(logged_in, now=NOW + 600)
    assert result.events == []
    user = await storage.get_user(logged_in)
    new_session = next(c.value for c in vault.open(user.session_blob) if c.name == "MoodleSession")
    assert new_session != "good-session" and new_session in moodle.valid_sessions


async def test_need_login_when_sso_session_also_expired(sync, logged_in, moodle):
    moodle.kill_sessions()
    moodle.sso_sessions.clear()
    with pytest.raises(NeedLogin):
        await sync.sync_user(logged_in, now=NOW)


# ---- напоминания --------------------------------------------------------------------------


async def test_reminders_thresholds_and_reschedule(sync, logged_in, moodle):
    await sync.sync_user(logged_in, now=NOW)
    lab = "assign:201"  # срок NOW + 72 ч, пороги по умолчанию 24 и 3 ч

    assert lab not in _reminded(await sync.due_reminders(now=NOW + 47 * H))
    assert _reminded(await sync.due_reminders(now=NOW + 49 * H))[lab] == 24
    assert lab not in _reminded(await sync.due_reminders(now=NOW + 50 * H))  # только один раз
    assert _reminded(await sync.due_reminders(now=NOW + 69 * H + 60))[lab] == 3

    # Срок перенесли — напоминания начинаются заново для нового срока
    moodle.events = [e for e in moodle.events if e["instance"] != 201] + [
        event(5, "assign", 201, "due", NOW + 100 * H, "Лабораторная 1", (20, "Физика"), 2002)
    ]
    await sync.sync_user(logged_in, now=NOW + 70 * H)
    assert _reminded(await sync.due_reminders(now=NOW + 77 * H))[lab] == 24


async def test_after_downtime_only_the_nearest_reminder(sync, logged_in):
    await sync.sync_user(logged_in, now=NOW)
    # Бот «проспал» оба порога лабораторной: придёт одно напоминание (3 ч), а не два
    due = await sync.due_reminders(now=NOW + 70 * H)
    lab = [e for e in due[logged_in] if isinstance(e, Reminder) and e.activity.key == "assign:201"]
    assert [e.hours for e in lab] == [3]


async def test_quiz_opening_notification(sync, logged_in):
    await sync.sync_user(logged_in, now=NOW)
    assert not any(
        isinstance(e, QuizOpened) for e in (await sync.due_reminders(now=NOW + 23 * H)).get(logged_in, [])
    )
    opened = (await sync.due_reminders(now=NOW + 24 * H + 30)).get(logged_in, [])
    assert [e.activity.name for e in opened if isinstance(e, QuizOpened)] == ["Тест по механике"]
    again = (await sync.due_reminders(now=NOW + 24 * H + 90)).get(logged_in, [])
    assert not any(isinstance(e, QuizOpened) for e in again)


async def test_new_activity_close_to_deadline_skips_passed_reminders(sync, logged_in, moodle):
    await sync.sync_user(logged_in, now=NOW)
    moodle.events.append(
        event(9, "assign", 209, "due", NOW + 5 * H, "Срочное ДЗ", (10, "Математический анализ"), 1009)
    )
    result = await sync.sync_user(logged_in, now=NOW + 60)
    assert any(isinstance(e, NewActivity) for e in result.events)
    # Порог «за сутки» уже позади — о сроке сказали в сообщении о новом задании
    assert "assign:209" not in _reminded(await sync.due_reminders(now=NOW + 120))
    assert _reminded(await sync.due_reminders(now=NOW + 2 * H + 60))["assign:209"] == 3


def _reminded(due):
    return {e.activity.key: e.hours for events in due.values() for e in events if isinstance(e, Reminder)}


async def test_session_dying_mid_sync_loses_no_events(sync, logged_in, moodle):
    await sync.sync_user(logged_in, now=NOW)
    moodle.events.append(event(9, "assign", 209, "due", NOW + 30 * H, "Новое ДЗ", (20, "Физика"), 2009))
    moodle.overview = [(10, "Математический анализ", "15,00"), (20, "Физика", "-")]
    # Сессия умирает ровно на загрузке отчёта об оценках — уже после заданий и материалов
    moodle.expire_on = "/grade/report/user/index.php"
    result = await sync.sync_user(logged_in, now=NOW + 600)
    assert {type(e).__name__ for e in result.events} >= {"NewActivity", "GradeChanged"}
