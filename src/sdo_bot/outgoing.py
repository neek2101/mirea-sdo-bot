from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Outgoing:
    """Сообщение пользователю: текст в HTML-разметке и необязательная кнопка."""

    text: str
    # (подпись, цель): цель — ссылка http(s) или callback_data для кнопки в боте
    button: tuple[str, str] | None = None
    # После этого момента сообщение уже неактуально (например, напоминание о прошедшем сроке)
    expires_at: int | None = None
