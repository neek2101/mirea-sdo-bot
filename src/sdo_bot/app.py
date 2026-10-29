"""Сборка зависимостей приложения в одном месте."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings
from .moodle import MoodleAPI, StoredCookie
from .moodle.models import SiteInfo
from .moodle.sso import SSO_HOSTS, SsoLogin
from .scheduler import Notifier, Scheduler
from .security import Vault
from .storage import Storage
from .sync import SyncService

LOGIN_TTL = 600  # сколько ждём код из письма, секунд


class LoginRegistry:
    """Незавершённые входы (между паролем и кодом из письма). Только в памяти."""

    def __init__(self) -> None:
        self._items: dict[int, tuple[SsoLogin, float]] = {}

    async def put(self, tg_id: int, login: SsoLogin) -> None:
        await self.drop(tg_id)
        await self._expire()
        self._items[tg_id] = (login, time.monotonic())

    def get(self, tg_id: int) -> SsoLogin | None:
        item = self._items.get(tg_id)
        if item and time.monotonic() - item[1] < LOGIN_TTL:
            return item[0]
        return None

    async def drop(self, tg_id: int) -> None:
        item = self._items.pop(tg_id, None)
        if item:
            await item[0].close()

    async def _expire(self) -> None:
        now = time.monotonic()
        for tg_id in [k for k, (_, t) in self._items.items() if now - t >= LOGIN_TTL]:
            await self.drop(tg_id)


@dataclass
class App:
    settings: Settings
    storage: Storage
    vault: Vault
    sync: SyncService
    notifier: Notifier
    scheduler: Scheduler
    sso_factory: Callable[[], SsoLogin]
    logins: LoginRegistry = field(default_factory=LoginRegistry)

    async def activate_session(self, tg_id: int, cookies: list[StoredCookie]) -> SiteInfo:
        """Проверить сессию в СДО и сохранить её. SessionExpired — СДО сессию не приняла."""
        async with self.sync.client(cookies) as client:
            site = await MoodleAPI(client).site_info()
            fresh = [c for c in cookies if c.domain in SSO_HOSTS] + client.export_cookies()
        await self.storage.set_session(tg_id, self.vault.seal(fresh), site.user_id, site.full_name)
        return site
