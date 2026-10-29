"""Настройки из переменных окружения (и файла .env)."""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Москва живёт в UTC+3 без перехода на летнее время с 2014 года. Фиксированный сдвиг
# не зависит от наличия tzdata в контейнере.
MSK = timezone(timedelta(hours=3), "МСК")


class Settings(BaseSettings):
    # env_ignore_empty: строка «MIREA_PROXY=» в .env значит «не задано», а не пустой адрес
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    bot_token: SecretStr
    # base64-строка ≥32 байт. Из неё pymirea выводит ключ Fernet для шифрования сессий в БД.
    # Через запятую можно перечислить несколько ключей: первым шифруем, остальными только
    # расшифровываем (ротация без потери сессий).
    secret_key: SecretStr

    database_path: Path = Path("data/bot.sqlite3")
    moodle_url: str = "https://online-edu.mirea.ru"

    poll_interval_minutes: int = Field(15, ge=5)
    poll_concurrency: int = Field(3, ge=1, le=20)
    # Насколько далеко назад смотреть в календаре: чтобы увидеть уже открытые тесты
    events_lookback_days: int = Field(30, ge=1, le=180)

    # Прокси для запросов к МИРЭА (некоторые сервисы МИРЭА режут дата-центры)
    mirea_proxy: str | None = None
    # Прокси до api.telegram.org (нужен пакет aiohttp-socks: pip install "sdo-bot[proxy]")
    telegram_proxy: str | None = None

    # Telegram ID через запятую: могут удалять любые идеи (/delidea), например спам
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    log_level: str = "INFO"

    @field_validator("moodle_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(int(x) for x in v.replace(" ", "").split(",") if x)
        return v
