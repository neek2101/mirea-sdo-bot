"""Фоновая работа: регулярный опрос СДО, напоминания и доставка сообщений из очереди."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

import httpx
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .moodle import MoodleError
from .outgoing import Outgoing
from .storage import OutboxItem, Storage
from .sync import NeedLogin, SyncResult, SyncService

log = logging.getLogger(__name__)

SESSION_EXPIRED_TEXT = (
    "⚠️ <b>Сессия СДО закончилась</b>\n\nЧтобы снова получать уведомления, войди ещё раз — это займёт минуту."
)


class Delivery(enum.Enum):
    SENT = "sent"
    DROP = "drop"  # Telegram отверг именно это сообщение — повторять бессмысленно
    BLOCKED = "blocked"  # пользователь заблокировал бота
    LATER = "later"  # Telegram сейчас недоступен — сообщение остаётся в очереди


class Notifier:
    """Доставляет сообщения из очереди. Сообщение удаляется, только когда Telegram его принял."""

    def __init__(self, bot: Bot, storage: Storage, *, retry_delay: float = 1.0):
        self.bot = bot
        self.storage = storage
        self.retry_delay = retry_delay
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def deliver(self, tg_id: int | None = None) -> None:
        users = [tg_id] if tg_id is not None else sorted({i.tg_id for i in await self.storage.outbox()})
        for tg in users:
            async with self._locks[tg]:  # без двойной отправки из разных задач
                for item in await self.storage.outbox(tg):
                    if item.expires_at <= time.time():
                        await self.storage.outbox_done(item.id)
                        continue
                    status = await self._send(item)
                    if status in (Delivery.SENT, Delivery.DROP):
                        await self.storage.outbox_done(item.id)
                        await asyncio.sleep(0.05)  # бережём лимиты Telegram
                    elif status is Delivery.BLOCKED:
                        await self.storage.outbox_drop_user(tg)
                        await self.storage.set_state(tg, "blocked")
                        break
                    else:
                        return  # Telegram недоступен — остальных не мучаем, повторим позже

    async def _send(self, item: OutboxItem) -> Delivery:
        markup = _markup(item.button_text, item.button_target)
        for attempt in range(3):
            try:
                await self.bot.send_message(item.tg_id, item.text, reply_markup=markup)
                return Delivery.SENT
            except TelegramRetryAfter as exc:
                if exc.retry_after > 30:
                    return Delivery.LATER
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                log.info("user %s blocked the bot", item.tg_id)
                return Delivery.BLOCKED
            except (TelegramNetworkError, TelegramServerError) as exc:
                log.warning("telegram unavailable (%s), attempt %s", type(exc).__name__, attempt + 1)
                await asyncio.sleep(self.retry_delay * 2**attempt)
            except TelegramBadRequest as exc:
                log.warning("message to %s rejected: %s", item.tg_id, exc.message)
                return Delivery.DROP
        return Delivery.LATER


def _markup(text: str | None, target: str | None) -> InlineKeyboardMarkup | None:
    if not text or not target:
        return None
    if target.startswith(("http://", "https://")):
        button = InlineKeyboardButton(text=text, url=target)
    else:
        button = InlineKeyboardButton(text=text, callback_data=target)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


class Scheduler:
    def __init__(
        self,
        sync: SyncService,
        storage: Storage,
        notifier: Notifier,
        *,
        interval_minutes: int,
        concurrency: int,
    ):
        self.sync = sync
        self.storage = storage
        self.notifier = notifier
        self.interval = interval_minutes * 60
        self.semaphore = asyncio.Semaphore(concurrency)
        self._cycle = 0
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(self.poll_all, self.interval, "poll")),
            asyncio.create_task(self._loop(self.send_reminders, 60, "reminders")),
            asyncio.create_task(self._loop(self.notifier.deliver, 30, "outbox")),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, job: Callable[[], Awaitable[None]], every: float, name: str) -> None:
        while True:
            started = time.monotonic()
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:  # фоновая задача не должна умирать из-за одной ошибки
                log.exception("background job %s failed", name)
            await asyncio.sleep(max(5.0, every - (time.monotonic() - started)))

    async def poll_all(self) -> None:
        self._cycle += 1
        users = await self.storage.users_in_state("active")
        due = [u for u in users if self._due(u.fail_count)]

        async def one(tg_id: int, delay: float) -> None:
            await asyncio.sleep(delay)  # размазываем запросы к СДО по времени
            async with self.semaphore:
                try:
                    await self.sync_and_notify(tg_id)
                except Exception:  # ошибка у одного пользователя не мешает остальным
                    log.exception("user %s: unexpected sync error", tg_id)

        spread = min(self.interval / 3, 2.0 * len(due))
        await asyncio.gather(*(one(u.tg_id, spread * i / max(len(due), 1)) for i, u in enumerate(due)))

    def _due(self, fail_count: int) -> bool:
        """После ошибок опрашиваем реже: каждый 2-й, 4-й, 8-й цикл."""
        if fail_count <= 0:
            return True
        return self._cycle % min(2 ** min(fail_count, 3), 8) == 0

    async def sync_and_notify(self, tg_id: int) -> SyncResult | None:
        try:
            result = await self.sync.sync_user(tg_id)
        except NeedLogin as exc:
            await self._session_lost(tg_id, str(exc))
            return None
        except (httpx.HTTPError, MoodleError) as exc:
            log.warning("user %s: sync failed: %s", tg_id, exc)
            await self.storage.record_failure(tg_id, str(exc))
            return None
        await self.notifier.deliver(tg_id)
        return result

    async def _session_lost(self, tg_id: int, reason: str) -> None:
        user = await self.storage.get_user(tg_id)
        if user is None or user.state != "active":
            return
        log.info("user %s: session lost (%s)", tg_id, reason)
        await self.storage.set_state(tg_id, "expired", reason)
        await self.storage.enqueue(tg_id, [Outgoing(SESSION_EXPIRED_TEXT, ("🔐 Войти", "login"))])
        await self.notifier.deliver(tg_id)

    async def send_reminders(self) -> None:
        due = await self.sync.due_reminders()
        for tg_id in due:
            await self.notifier.deliver(tg_id)
