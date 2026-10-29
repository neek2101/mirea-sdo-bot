"""Синхронизация с СДО: снять снимок, сравнить с прошлым, выдать события.

Порядок работы опроса:
1. прочитать прошлый снимок из БД;
2. скачать всё нужное из СДО (только сеть, никаких записей);
3. сравнить в памяти — чистые функции без ввода-вывода;
4. одной транзакцией записать новый снимок вместе с сообщениями в очередь отправки.

Поэтому ни обрыв сессии посреди опроса, ни недоступный Telegram, ни падение процесса
не приводят к «учтённому, но не отправленному» событию.

И главное правило сравнения: неудачный ответ никогда не считается «всё исчезло».
Каждый источник (задания, материалы, оценки, уведомления) сравнивается, только если
получен целиком.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TypeVar

import httpx

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
from .moodle import MoodleAPI, MoodleClient, MoodleError, SessionExpired, StoredCookie, silent_relogin
from .moodle.models import Activity, Course, CourseGrade, GradeItem, Module, Notification, SiteInfo
from .moodle.sso import SSO_HOSTS
from .render import render_events
from .security import Vault
from .storage import Changes, Mark, Storage, SyncState, User

log = logging.getLogger(__name__)

CMID_RE = re.compile(r"[?&]id=(\d+)")
T = TypeVar("T")


GROUP_RECHECK = 7 * 86400  # группу перепроверяем раз в неделю: вдруг перевёлся


class NeedLogin(Exception):
    """Сохранённой сессии нет или она окончательно истекла."""


@dataclass
class Snapshot:
    site: SiteInfo | None = None
    courses: list[Course] | None = None
    activities: list[Activity] | None = None
    modules: dict[int, list[Module]] = field(default_factory=dict)
    overview: list[CourseGrade] | None = None
    reports: dict[int, list[GradeItem] | None] = field(default_factory=dict)
    notifications: list[Notification] | None = None
    group: str | None = None
    group_checked: bool = False  # группу проверяли в этом опросе
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    events: list[Event]
    initial: bool
    errors: list[str]


class SyncService:
    def __init__(
        self,
        storage: Storage,
        vault: Vault,
        *,
        moodle_url: str,
        lookback_days: int = 30,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.storage = storage
        self.vault = vault
        self.moodle_url = moodle_url.rstrip("/")
        self.lookback_days = lookback_days
        self.proxy = proxy
        self.transport = transport  # для тестов
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---- сессия ----------------------------------------------------------------------------

    def client(self, cookies: list[StoredCookie]) -> MoodleClient:
        return MoodleClient(self.moodle_url, cookies, proxy=self.proxy, transport=self.transport)

    @asynccontextmanager
    async def api(self, user: User) -> AsyncIterator[MoodleAPI]:
        """Рабочая сессия СДО. Если сессия Moodle истекла — один раз пробуем молча
        перевойти через сохранённую SSO-сессию; иначе NeedLogin."""
        stored = self.vault.open(user.session_blob)
        if not stored:
            raise NeedLogin("нет сохранённой сессии")
        client = self.client(stored)
        try:
            try:
                await client.ensure_sesskey()
            except SessionExpired:
                await client.close()
                stored = await self._relogin(user, stored)
                client = self.client(stored)
                try:
                    await client.ensure_sesskey()
                except SessionExpired as exc:
                    raise NeedLogin("СДО не приняла новую сессию") from exc
            yield MoodleAPI(client)
            await self._persist_cookies(user, stored, client.export_cookies())
        finally:
            await client.close()

    async def _relogin(self, user: User, stored: list[StoredCookie]) -> list[StoredCookie]:
        fresh = await silent_relogin(self.moodle_url, stored, proxy=self.proxy, transport=self.transport)
        if not fresh:
            raise NeedLogin("сессия СДО и единого входа истекла")
        log.info("user %s: silently re-logged in", user.tg_id)
        user.session_blob = self.vault.seal(fresh)
        await self.storage.update_session_blob(user.tg_id, user.session_blob)
        return fresh

    async def _persist_cookies(
        self, user: User, stored: list[StoredCookie], moodle_cookies: list[StoredCookie]
    ) -> None:
        """Moodle может обновить cookie по ходу работы — сохраняем актуальные."""
        if not moodle_cookies:
            return
        merged = [c for c in stored if c.domain in SSO_HOSTS] + moodle_cookies
        if set(merged) != set(stored):
            user.session_blob = self.vault.seal(merged)
            await self.storage.update_session_blob(user.tg_id, user.session_blob)

    # ---- опрос -----------------------------------------------------------------------------

    async def sync_user(self, tg_id: int, now: int | None = None) -> SyncResult:
        async with self._locks[tg_id]:
            user = await self.storage.get_user(tg_id)
            if user is None or user.state != "active":
                raise NeedLogin("пользователь не вошёл")
            now = now or int(time.time())
            state = await self.storage.load_state(tg_id)
            try:
                snap = await self._fetch(user, state, now)
            except SessionExpired:
                # Сессия умерла посреди опроса. В БД ещё ничего не записано, поэтому
                # можно спокойно перевойти и повторить опрос целиком — ровно один раз.
                await self._relogin(user, self.vault.open(user.session_blob) or [])
                snap = await self._fetch(user, state, now)

            initial = not user.initialized
            events, changes = diff(snap, state, user, now, initial)
            await self.storage.apply_sync(tg_id, changes, render_events(events, user.settings, now), now)
            if snap.group_checked:
                await self.storage.set_group(tg_id, snap.group, now)
            if snap.errors:
                log.warning("user %s: partial sync: %s", tg_id, snap.errors)
            return SyncResult(events, initial, snap.errors)

    async def _fetch(self, user: User, state: SyncState, now: int) -> Snapshot:
        async with self.api(user) as api:
            return await self._collect(api, user, state, now)

    async def _collect(self, api: MoodleAPI, user: User, state: SyncState, now: int) -> Snapshot:
        snap = Snapshot()

        async def attempt(label: str, coro: Awaitable[T]) -> T | None:
            try:
                return await coro
            except SessionExpired:
                raise
            except (MoodleError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                snap.errors.append(f"{label}: {exc}")
                return None

        snap.site = await attempt("профиль", api.site_info())
        snap.courses = await attempt("курсы", api.courses())
        snap.activities = await attempt("задания", api.activities(now - self.lookback_days * 86400))
        if snap.courses:
            mods = await attempt("материалы", api.course_modules([c.id for c in snap.courses]))
            for cid, res in (mods or {}).items():
                if isinstance(res, MoodleError):
                    snap.errors.append(f"материалы курса {cid}: {res}")
                else:
                    snap.modules[cid] = res
        snap.overview = await attempt("оценки", api.grade_overview())
        initial = not user.initialized
        for cg in snap.overview or []:
            old_total = state.grades.get((cg.course_id, ""))
            # Детальный отчёт нужен для точки отсчёта и когда итог курса изменился
            if initial or old_total is None or old_total != cg.grade:
                snap.reports[cg.course_id] = await attempt(
                    f"оценки курса {cg.course_id}", api.course_grade_items(cg.course_id)
                )
        user_id = snap.site.user_id if snap.site else user.moodle_user_id
        if user_id:
            snap.notifications = await attempt("уведомления", api.notifications(user_id))
        if snap.courses and (user.group_name is None or now - (user.group_checked_at or 0) > GROUP_RECHECK):
            snap.group = await attempt("группа", api.my_group([c.id for c in snap.courses]))
            snap.group_checked = True
        return snap

    # ---- напоминания (без сети, по сохранённым данным) ------------------------------------

    async def due_reminders(self, now: int | None = None) -> dict[int, list[Event]]:
        now = now or int(time.time())
        by_user: dict[int, list[Activity]] = defaultdict(list)
        for tg, a in await self.storage.pending_activities():
            by_user[tg].append(a)
        for tg, a in await self.storage.pending_group_activities(now):
            by_user[tg].append(a)
        result: dict[int, list[Event]] = {}
        for tg, acts in by_user.items():
            user = await self.storage.get_user(tg)
            if user is None:
                continue
            events: list[Event] = []
            marks: list[Mark] = []
            for a in acts:
                for kind, ts, event in _time_events(a, user.settings.reminders, now):
                    if not await self.storage.has_mark(tg, a.key, kind, ts):
                        events.append(event)
                        # Все более ранние пороги считаем пройденными: без лавины после простоя
                        marks += already_due_marks(a, user.settings.reminders, now)
            if events:
                await self.storage.apply_marks(tg, marks, render_events(events, user.settings, now), now)
                result[tg] = events
        return result


# ---- сравнение снимков: чистые функции ------------------------------------------------------


def diff(
    snap: Snapshot, state: SyncState, user: User, now: int, initial: bool
) -> tuple[list[Event], Changes]:
    ch = Changes(error="; ".join(snap.errors)[:500] or None)
    events: list[Event] = []
    events += _diff_activities(snap, state, user, now, initial, ch)
    events += _diff_materials(snap, state, initial, events, ch)
    events += _diff_grades(snap, state, initial, ch)
    events += _diff_notifications(snap, user, initial, ch)
    if initial and snap.activities is not None:
        active, upcoming, overdue = split_activities(snap.activities, now)
        events.insert(
            0,
            Welcome(
                active=active,
                upcoming=upcoming,
                overdue=overdue,
                courses=len(snap.courses or []),
                full_name=(snap.site.full_name if snap.site else user.full_name) or "",
            ),
        )
    # Первичный снимок засчитываем, только если задания получены: иначе приветственную
    # сводку не из чего строить, и следующий опрос повторит первичную синхронизацию.
    ch.initialized = snap.activities is not None
    return events, ch


def _diff_activities(
    snap: Snapshot, state: SyncState, user: User, now: int, initial: bool, ch: Changes
) -> list[Event]:
    if snap.activities is None:
        return []
    current = {a.key: a for a in snap.activities}
    events: list[Event] = []
    for a in current.values():
        old = state.activities.get(a.key)
        informed = initial
        if not initial:
            if old is None:
                events.append(NewActivity(a))
                informed = True
            elif old.activity.close_ts != a.close_ts and a.close_ts is not None:
                events.append(DeadlineChanged(a, old.activity.close_ts))
                informed = True
        if informed:
            # Пользователь только что увидел срок — не дублируем «уже наступившими»
            # напоминаниями и сообщением об открытии теста.
            ch.marks += already_due_marks(a, user.settings.reminders, now)
    ch.activities = list(current.values())
    # Сданное Moodle больше не показывает среди «требующих действия»
    ch.done = [k for k, s in state.activities.items() if s.status == "pending" and k not in current]
    return events


def _diff_materials(
    snap: Snapshot, state: SyncState, initial: bool, so_far: list[Event], ch: Changes
) -> list[Event]:
    if not snap.courses:
        return []
    # Новые тесты/задания уже объявлены отдельным сообщением — не повторяем их в материалах
    announced = {
        int(m.group(1))
        for e in so_far
        if isinstance(e, NewActivity) and (m := CMID_RE.search(e.activity.url))
    }
    events: list[Event] = []
    for course in snap.courses:
        if course.id not in snap.modules:
            continue  # материалы курса не получены — сравнивать не с чем
        mods = snap.modules[course.id]
        # Пустой known_courses — точки отсчёта ещё не было (первые опросы не удались):
        # тогда это не «новые курсы», а первая успешная фиксация.
        if not initial and state.known_courses:
            if course.id not in state.known_courses:
                events.append(NewCourse(course))
            else:
                prev = state.modules.get(course.id, set())
                fresh = [m for m in mods if m.cmid not in prev and m.cmid not in announced]
                if fresh:
                    events.append(NewMaterials(course, fresh))
        ch.modules[course.id] = mods
    return events


def _diff_grades(snap: Snapshot, state: SyncState, initial: bool, ch: Changes) -> list[Event]:
    if snap.overview is None:
        return []
    events: list[Event] = []
    for cg in snap.overview:
        old_total = state.grades.get((cg.course_id, ""))
        items = snap.reports.get(cg.course_id)
        baseline = initial or old_total is None
        if not baseline and old_total != cg.grade:
            events += _grade_events(cg, old_total, items, state.grades)
        if items is not None:
            ch.grades += [(cg.course_id, it.key, it.name, it.grade) for it in items]
        ch.grades.append((cg.course_id, "", cg.course_name, cg.grade))
    return events


def _diff_notifications(snap: Snapshot, user: User, initial: bool, ch: Changes) -> list[Event]:
    if snap.notifications is None:
        return []
    ch.last_notification_id = max((n.id for n in snap.notifications), default=0)
    # None — точки отсчёта ещё нет: первый раз только запоминаем, ничего не шлём
    if initial or user.last_notification_id is None:
        return []
    fresh = sorted((n for n in snap.notifications if n.id > user.last_notification_id), key=lambda n: n.id)
    return [MoodleNotice(n) for n in fresh]


def _grade_events(
    cg: CourseGrade,
    old_total: str | None,
    items: list[GradeItem] | None,
    stored: dict[tuple[int, str], str],
) -> list[Event]:
    total = cg.grade if cg.grade not in ("", "-") else None
    if items is None:
        return [GradeChanged(cg.course_id, cg.course_name, None, old_total, cg.grade)]
    has_baseline = any(cid == cg.course_id and key for cid, key in stored)
    events: list[Event] = []
    for it in items:
        if it.is_total or not it.has_grade:
            continue
        old = stored.get((cg.course_id, it.key))
        if old == it.grade or (old is None and not has_baseline):
            continue
        events.append(GradeChanged(cg.course_id, cg.course_name, it.name, old, it.grade, it.range, total))
    if not events and old_total != cg.grade:
        events.append(GradeChanged(cg.course_id, cg.course_name, None, old_total, cg.grade))
    return events


def _time_events(a: Activity, thresholds: list[int], now: int) -> list[tuple[str, int, Event]]:
    """Наступившие к `now` события по времени: открытие и ближайший порог напоминания."""
    out: list[tuple[str, int, Event]] = []
    if a.open_ts and a.open_ts <= now and (a.close_ts is None or a.close_ts > now):
        out.append(("opened", a.open_ts, QuizOpened(a)))
    if a.close_ts and a.close_ts > now and (a.open_ts is None or a.open_ts <= now):
        due = [h for h in sorted(thresholds) if a.close_ts - now <= h * 3600]
        if due:
            out.append((f"remind:{due[0]}", a.close_ts, Reminder(a, due[0])))
    return out


def already_due_marks(a: Activity, thresholds: list[int], now: int) -> list[Mark]:
    marks: list[Mark] = []
    if a.open_ts and a.open_ts <= now:
        marks.append((a.key, "opened", a.open_ts))
    if a.close_ts and (a.open_ts is None or a.open_ts <= now):
        marks += [(a.key, f"remind:{h}", a.close_ts) for h in thresholds if a.close_ts - now <= h * 3600]
    return marks


def split_activities(acts: list[Activity], now: int) -> tuple[list[Activity], list[Activity], list[Activity]]:
    """(идут сейчас, откроются позже, просрочены)."""
    active, upcoming, overdue = [], [], []
    for a in acts:
        if a.close_ts is not None and a.close_ts <= now:
            overdue.append(a)
        elif a.open_ts is not None and a.open_ts > now:
            upcoming.append(a)
        else:
            active.append(a)
    active.sort(key=lambda a: a.close_ts or 2**62)
    upcoming.sort(key=lambda a: a.open_ts or 0)
    overdue.sort(key=lambda a: a.close_ts or 0)
    return active, upcoming, overdue
