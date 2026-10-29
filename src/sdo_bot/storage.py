"""Хранилище SQLite: пользователи, их настройки, последний «снимок» СДО и очередь сообщений.

Изменения снимка и сообщения о них записываются одной транзакцией (apply_sync): либо
сохраняется и то и другое, либо ничего — событие не может «потеряться» между ними.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiosqlite

from .moodle.models import Activity, Module
from .outgoing import Outgoing

SCHEMA_VERSION = 1
OUTBOX_TTL = 3 * 86400  # обычные сообщения живут в очереди трое суток

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id           INTEGER PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'new',   -- new | active | expired | blocked
    session_blob    TEXT,                          -- зашифрованные cookie СДО и SSO
    moodle_user_id  INTEGER,
    full_name       TEXT,
    initialized     INTEGER NOT NULL DEFAULT 0,    -- снимок СДО уже сделан
    last_sync_at    INTEGER,
    last_error      TEXT,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_notification_id INTEGER,                  -- NULL — точка отсчёта ещё не задана
    settings        TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS activities (
    tg_id       INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    modname     TEXT NOT NULL,
    name        TEXT NOT NULL,
    course_id   INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    url         TEXT NOT NULL,
    open_ts     INTEGER,
    close_ts    INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | done
    PRIMARY KEY (tg_id, key)
);
-- Что уже отправлено: напоминания, «тест открыт». ts — срок, к которому относится отметка,
-- поэтому после переноса срока напоминания придут заново.
CREATE TABLE IF NOT EXISTS sent_marks (
    tg_id   INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (tg_id, key, kind, ts)
);
CREATE TABLE IF NOT EXISTS modules (
    tg_id     INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    cmid      INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (tg_id, cmid)
);
CREATE TABLE IF NOT EXISTS known_courses (
    tg_id     INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL,
    PRIMARY KEY (tg_id, course_id)
);
CREATE TABLE IF NOT EXISTS grades (
    tg_id     INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL,
    item_key  TEXT NOT NULL,                       -- '' — итог курса из обзорного отчёта
    name      TEXT NOT NULL,
    grade     TEXT NOT NULL,
    PRIMARY KEY (tg_id, course_id, item_key)
);
-- Очередь исходящих: сообщение удаляется только после того, как Telegram его принял
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    button_text TEXT,
    button_target TEXT,                            -- ссылка или callback_data
    expires_at  INTEGER NOT NULL
);
"""

DATA_TABLES = ("activities", "sent_marks", "modules", "known_courses", "grades", "outbox")
REMINDER_CHOICES = (72, 24, 12, 6, 3, 1)

Mark = tuple[str, str, int]  # (ключ задания, вид отметки, срок)


@dataclass
class UserSettings:
    new_items: bool = True  # новые задания и тесты
    openings: bool = True  # тест открылся
    deadline_changes: bool = True
    reminders: list[int] = field(default_factory=lambda: [24, 3])  # за сколько часов до срока
    materials: bool = True
    grades: bool = True
    moodle_notifications: bool = True  # уведомления самой СДО (колокольчик)

    @classmethod
    def from_json(cls, raw: str | None) -> UserSettings:
        data = json.loads(raw or "{}")
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class User:
    tg_id: int
    state: str
    session_blob: str | None
    moodle_user_id: int | None
    full_name: str | None
    initialized: bool
    last_sync_at: int | None
    last_error: str | None
    fail_count: int
    last_notification_id: int | None
    settings: UserSettings


@dataclass
class StoredActivity:
    activity: Activity
    status: str


@dataclass
class SyncState:
    """Всё, что нужно для сравнения со свежим снимком СДО."""

    activities: dict[str, StoredActivity]
    known_courses: set[int]
    modules: dict[int, set[int]]
    grades: dict[tuple[int, str], str]


@dataclass
class Changes:
    """Изменения снимка по итогам одного опроса. None — источник не получен, не трогаем."""

    activities: list[Activity] | None = None
    done: list[str] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)
    modules: dict[int, list[Module]] = field(default_factory=dict)
    grades: list[tuple[int, str, str, str]] = field(default_factory=list)  # курс, ключ, имя, оценка
    last_notification_id: int | None = None
    initialized: bool = False  # первичный снимок состоялся
    error: str | None = None


@dataclass(frozen=True)
class OutboxItem:
    id: int
    tg_id: int
    text: str
    button_text: str | None
    button_target: str | None
    expires_at: int


class Storage:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        # Соединение одно на всё приложение: транзакции не должны перемешиваться
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path | str) -> Storage:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self.db.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            try:
                yield self.db
            except BaseException:
                await self.db.rollback()
                raise
            await self.db.commit()

    # ---- пользователи ---------------------------------------------------------------------

    async def ensure_user(self, tg_id: int) -> User:
        async with self.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (tg_id, created_at) VALUES (?, ?)", (tg_id, int(time.time()))
            )
        user = await self.get_user(tg_id)
        assert user is not None
        return user

    async def get_user(self, tg_id: int) -> User | None:
        async with self.db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return _user(row) if row else None

    async def users_in_state(self, state: str) -> list[User]:
        async with self.db.execute("SELECT * FROM users WHERE state = ?", (state,)) as cur:
            return [_user(r) for r in await cur.fetchall()]

    async def set_session(self, tg_id: int, blob: str, moodle_user_id: int, full_name: str) -> None:
        """Сохранить новую сессию. Если вошёл другой аккаунт СДО — снимок начинаем заново."""
        user = await self.ensure_user(tg_id)
        reset = user.moodle_user_id not in (None, moodle_user_id)
        async with self.transaction() as db:
            if reset:
                for table in DATA_TABLES:
                    await db.execute(f"DELETE FROM {table} WHERE tg_id = ?", (tg_id,))
            await db.execute(
                """UPDATE users SET session_blob = ?, moodle_user_id = ?, full_name = ?, state = 'active',
                   last_error = NULL, fail_count = 0,
                   initialized = CASE WHEN ? THEN 0 ELSE initialized END,
                   last_notification_id = CASE WHEN ? THEN NULL ELSE last_notification_id END
                   WHERE tg_id = ?""",
                (blob, moodle_user_id, full_name, reset, reset, tg_id),
            )

    async def update_session_blob(self, tg_id: int, blob: str) -> None:
        async with self.transaction() as db:
            await db.execute("UPDATE users SET session_blob = ? WHERE tg_id = ?", (blob, tg_id))

    async def set_state(self, tg_id: int, state: str, error: str | None = None) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE users SET state = ?, last_error = ? WHERE tg_id = ?", (state, error, tg_id)
            )

    async def record_failure(self, tg_id: int, error: str) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE users SET last_error = ?, fail_count = fail_count + 1 WHERE tg_id = ?",
                (error[:500], tg_id),
            )

    async def save_settings(self, tg_id: int, settings: UserSettings) -> None:
        async with self.transaction() as db:
            await db.execute("UPDATE users SET settings = ? WHERE tg_id = ?", (settings.to_json(), tg_id))

    async def delete_user(self, tg_id: int) -> None:
        async with self.transaction() as db:
            await db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))

    # ---- снимок СДО ----------------------------------------------------------------------

    async def activities(self, tg_id: int) -> dict[str, StoredActivity]:
        async with self.db.execute("SELECT * FROM activities WHERE tg_id = ?", (tg_id,)) as cur:
            rows = await cur.fetchall()
        return {r["key"]: StoredActivity(_activity(r), r["status"]) for r in rows}

    async def load_state(self, tg_id: int) -> SyncState:
        modules: dict[int, set[int]] = defaultdict(set)
        async with self.db.execute("SELECT course_id, cmid FROM modules WHERE tg_id = ?", (tg_id,)) as cur:
            for cid, cmid in await cur.fetchall():
                modules[cid].add(cmid)
        async with self.db.execute("SELECT course_id FROM known_courses WHERE tg_id = ?", (tg_id,)) as cur:
            known = {r[0] for r in await cur.fetchall()}
        async with self.db.execute(
            "SELECT course_id, item_key, grade FROM grades WHERE tg_id = ?", (tg_id,)
        ) as cur:
            grades = {(r[0], r[1]): r[2] for r in await cur.fetchall()}
        return SyncState(await self.activities(tg_id), known, dict(modules), grades)

    async def apply_sync(self, tg_id: int, ch: Changes, outgoing: Iterable[Outgoing], now: int) -> None:
        """Записать изменения снимка и сообщения о них атомарно."""
        async with self.transaction() as db:
            if ch.activities is not None:
                await db.executemany(
                    """INSERT INTO activities (tg_id, key, modname, name, course_id, course_name, url,
                                               open_ts, close_ts, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                       ON CONFLICT (tg_id, key) DO UPDATE SET
                         modname = excluded.modname, name = excluded.name,
                         course_id = excluded.course_id, course_name = excluded.course_name,
                         url = excluded.url, open_ts = excluded.open_ts, close_ts = excluded.close_ts,
                         status = 'pending'""",
                    [
                        (
                            tg_id,
                            a.key,
                            a.modname,
                            a.name,
                            a.course_id,
                            a.course_name,
                            a.url,
                            a.open_ts,
                            a.close_ts,
                        )
                        for a in ch.activities
                    ],
                )
            await db.executemany(
                "UPDATE activities SET status = 'done' WHERE tg_id = ? AND key = ?",
                [(tg_id, k) for k in ch.done],
            )
            await self._insert_marks(db, tg_id, ch.marks)
            for course_id, mods in ch.modules.items():
                await db.execute(
                    "INSERT OR IGNORE INTO known_courses (tg_id, course_id) VALUES (?, ?)", (tg_id, course_id)
                )
                await db.executemany(
                    "INSERT OR REPLACE INTO modules (tg_id, cmid, course_id, name) VALUES (?, ?, ?, ?)",
                    [(tg_id, m.cmid, course_id, m.name) for m in mods],
                )
            await db.executemany(
                "INSERT OR REPLACE INTO grades (tg_id, course_id, item_key, name, grade) "
                "VALUES (?, ?, ?, ?, ?)",
                [(tg_id, *g) for g in ch.grades],
            )
            if ch.last_notification_id is not None:
                await db.execute(
                    "UPDATE users SET last_notification_id = MAX(COALESCE(last_notification_id, 0), ?) "
                    "WHERE tg_id = ?",
                    (ch.last_notification_id, tg_id),
                )
            await db.execute(
                """UPDATE users SET last_sync_at = ?, last_error = ?, fail_count = 0,
                   initialized = MAX(initialized, ?) WHERE tg_id = ?""",
                (now, ch.error, int(ch.initialized), tg_id),
            )
            await self._enqueue(db, tg_id, outgoing, now)

    async def apply_marks(
        self, tg_id: int, marks: list[Mark], outgoing: Iterable[Outgoing], now: int
    ) -> None:
        async with self.transaction() as db:
            await self._insert_marks(db, tg_id, marks)
            await self._enqueue(db, tg_id, outgoing, now)

    async def has_mark(self, tg_id: int, key: str, kind: str, ts: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM sent_marks WHERE tg_id = ? AND key = ? AND kind = ? AND ts = ?",
            (tg_id, key, kind, ts),
        ) as cur:
            return await cur.fetchone() is not None

    async def pending_activities(self) -> list[tuple[int, Activity]]:
        """Все незакрытые задания активных пользователей — для напоминаний."""
        async with self.db.execute(
            """SELECT a.* FROM activities a JOIN users u ON u.tg_id = a.tg_id
               WHERE a.status = 'pending' AND u.state = 'active' AND u.initialized = 1"""
        ) as cur:
            return [(r["tg_id"], _activity(r)) for r in await cur.fetchall()]

    async def course_name(self, tg_id: int, course_id: int) -> str | None:
        async with self.db.execute(
            "SELECT name FROM grades WHERE tg_id = ? AND course_id = ? AND item_key = ''", (tg_id, course_id)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    # ---- очередь сообщений --------------------------------------------------------------------

    async def enqueue(self, tg_id: int, outgoing: Iterable[Outgoing], now: int | None = None) -> None:
        async with self.transaction() as db:
            await self._enqueue(db, tg_id, outgoing, now or int(time.time()))

    async def outbox(self, tg_id: int | None = None) -> list[OutboxItem]:
        sql = "SELECT * FROM outbox"
        args: tuple[int, ...] = ()
        if tg_id is not None:
            sql += " WHERE tg_id = ?"
            args = (tg_id,)
        async with self.db.execute(sql + " ORDER BY id", args) as cur:
            return [
                OutboxItem(
                    r["id"], r["tg_id"], r["text"], r["button_text"], r["button_target"], r["expires_at"]
                )
                for r in await cur.fetchall()
            ]

    async def outbox_done(self, item_id: int) -> None:
        async with self.transaction() as db:
            await db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    async def outbox_drop_user(self, tg_id: int) -> None:
        async with self.transaction() as db:
            await db.execute("DELETE FROM outbox WHERE tg_id = ?", (tg_id,))

    @staticmethod
    async def _insert_marks(db: aiosqlite.Connection, tg_id: int, marks: list[Mark]) -> None:
        await db.executemany(
            "INSERT OR IGNORE INTO sent_marks (tg_id, key, kind, ts) VALUES (?, ?, ?, ?)",
            [(tg_id, key, kind, ts) for key, kind, ts in marks],
        )

    @staticmethod
    async def _enqueue(db: aiosqlite.Connection, tg_id: int, outgoing: Iterable[Outgoing], now: int) -> None:
        rows = [
            (
                tg_id,
                o.text,
                o.button[0] if o.button else None,
                o.button[1] if o.button else None,
                o.expires_at or now + OUTBOX_TTL,
            )
            for o in outgoing
        ]
        await db.executemany(
            "INSERT INTO outbox (tg_id, text, button_text, button_target, expires_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _activity(r: aiosqlite.Row) -> Activity:
    return Activity(
        key=r["key"],
        modname=r["modname"],
        name=r["name"],
        course_id=r["course_id"],
        course_name=r["course_name"],
        url=r["url"],
        open_ts=r["open_ts"],
        close_ts=r["close_ts"],
    )


def _user(r: aiosqlite.Row) -> User:
    return User(
        tg_id=r["tg_id"],
        state=r["state"],
        session_blob=r["session_blob"],
        moodle_user_id=r["moodle_user_id"],
        full_name=r["full_name"],
        initialized=bool(r["initialized"]),
        last_sync_at=r["last_sync_at"],
        last_error=r["last_error"],
        fail_count=r["fail_count"],
        last_notification_id=r["last_notification_id"],
        settings=UserSettings.from_json(r["settings"]),
    )
