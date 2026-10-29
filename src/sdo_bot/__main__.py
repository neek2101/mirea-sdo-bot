"""Точка входа: python -m sdo_bot"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from .app import App
from .bot import build_dispatcher
from .config import Settings
from .moodle.sso import SsoLogin
from .scheduler import Notifier, Scheduler
from .security import Vault
from .storage import Storage
from .sync import SyncService

log = logging.getLogger("sdo_bot")

COMMANDS = [
    BotCommand(command="deadlines", description="Что нужно сдать"),
    BotCommand(command="subjects", description="Дедлайны по предметам"),
    BotCommand(command="grades", description="Баллы по дисциплинам"),
    BotCommand(command="ideas", description="Идеи и предложения"),
    BotCommand(command="sync", description="Проверить СДО сейчас"),
    BotCommand(command="settings", description="Настройки уведомлений"),
    BotCommand(command="login", description="Войти в СДО"),
    BotCommand(command="logout", description="Выйти и удалить данные"),
    BotCommand(command="help", description="Помощь"),
]


async def run(settings: Settings) -> None:
    import pymirea

    secret = settings.secret_key.get_secret_value()
    pymirea.configure(pymirea.Config(session_keys=secret, mirea_proxy=settings.mirea_proxy))

    storage = await Storage.open(settings.database_path)
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    vault = Vault(secret)
    sync = SyncService(
        storage,
        vault,
        moodle_url=settings.moodle_url,
        lookback_days=settings.events_lookback_days,
        proxy=settings.mirea_proxy,
    )
    notifier = Notifier(bot, storage)
    scheduler = Scheduler(
        sync,
        storage,
        notifier,
        interval_minutes=settings.poll_interval_minutes,
        concurrency=settings.poll_concurrency,
    )
    app = App(
        settings=settings,
        storage=storage,
        vault=vault,
        sync=sync,
        notifier=notifier,
        scheduler=scheduler,
        sso_factory=lambda: SsoLogin(settings.moodle_url, proxy=settings.mirea_proxy),
    )

    dp = build_dispatcher()

    @dp.startup()
    async def on_startup() -> None:
        try:
            await bot.set_my_commands(COMMANDS)
        except TelegramAPIError as exc:  # меню команд — не повод не запускаться
            log.warning("set_my_commands failed: %s", exc)
        scheduler.start()
        log.info("bot started")

    @dp.shutdown()
    async def on_shutdown() -> None:
        await scheduler.stop()
        await storage.close()
        log.info("bot stopped")

    await wait_for_telegram(bot)
    await dp.start_polling(bot, app=app, allowed_updates=dp.resolve_used_update_types())


async def wait_for_telegram(bot: Bot) -> None:
    """aiogram запрашивает getMe один раз и без повторов: если Telegram в этот момент
    недоступен, процесс завершается. Ждём связь сами; дальше обрывы getUpdates aiogram
    переживает своим механизмом повторов, а сообщения ждут в очереди."""
    delay = 1.0
    while True:
        try:
            me = await bot.me()  # результат кешируется, повторно aiogram его не запросит
            log.info("connected to Telegram as @%s", me.username)
            return
        except (TelegramNetworkError, TelegramServerError) as exc:
            log.warning("Telegram unreachable (%s), retry in %.0f s", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx пишет каждый запрос с полным URL (в нём sesskey) — оставляем только предупреждения
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # pymirea на уровне INFO пишет фрагменты страниц Keycloak — в журнал им не место
    logging.getLogger("pymirea").setLevel(logging.WARNING)
    # httpx сам берёт системный прокси (HTTP(S)_PROXY и реестр Windows). VPN-клиенты вроде
    # Happ ставят его на всю систему, и запросы к МИРЭА уходят в VPN, где сайт недоступен.
    # Прокси задаём только явно: MIREA_PROXY и TELEGRAM_PROXY.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
