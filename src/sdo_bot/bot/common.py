"""Клавиатуры, общие тексты и доступ."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from ..render import fmt_hours
from ..storage import REMINDER_CHOICES, UserSettings

BTN_DEADLINES = "Все дедлайны"
BTN_BY_COURSE = "Дедлайны по предметам"
BTN_GRADES = "Оценки"
BTN_SYNC = "Обновить"
BTN_SETTINGS = "Настройки"
BTN_IDEAS = "Идеи и предложения"
BTN_HELP = "Помощь"
GITHUB_URL = "https://github.com/neek2101/mirea-sdo-bot"
MENU_BUTTONS = frozenset(
    {BTN_DEADLINES, BTN_BY_COURSE, BTN_GRADES, BTN_SYNC, BTN_IDEAS, BTN_SETTINGS, BTN_HELP}
)
# Подписи прежней клавиатуры: у пользователей она остаётся, пока не придёт новая
OLD_BUTTONS = {
    "📅 Сроки": BTN_DEADLINES,
    "📊 Оценки": BTN_GRADES,
    "🔄 Обновить": BTN_SYNC,
    "⚙️ Настройки": BTN_SETTINGS,
}

HELP_TEXT = (
    "🤖 <b>Что я умею</b>\n\n"
    "• Сообщаю о новых тестах и заданиях в СДО, об открытии тестов и переносе сроков.\n"
    "• Напоминаю о сроках сдачи (по умолчанию за сутки и за 3 часа).\n"
    "• Сообщаю о новых материалах в курсах и новых оценках.\n"
    "• Пересылаю уведомления самой СДО (колокольчик).\n\n"
    f"{BTN_DEADLINES} — что нужно сдать, по дисциплинам\n"
    f"{BTN_BY_COURSE} — сроки одной дисциплины\n"
    f"{BTN_GRADES} — баллы по дисциплинам\n"
    f"{BTN_SYNC} — проверить СДО прямо сейчас\n"
    f"{BTN_SETTINGS} — какие уведомления присылать\n\n"
    f"{BTN_IDEAS} — предложить идею или проголосовать за чужие\n\n"
    "/login — войти заново, /logout — выйти и удалить данные.\n\n"
    "🔒 Пароль я не храню: он нужен один раз, чтобы войти через единый вход МИРЭА. "
    "Сохраняется только зашифрованная сессия СДО.\n\n"
    f'Исходный код: <a href="{GITHUB_URL}">GitHub</a>'
)

SETTINGS_LABELS = {
    "new_items": "Новые тесты и задания",
    "openings": "Открытие тестов",
    "deadline_changes": "Перенос сроков",
    "materials": "Новые материалы",
    "grades": "Новые оценки",
    "moodle_notifications": "Уведомления СДО",
}


def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for text in (BTN_DEADLINES, BTN_BY_COURSE, BTN_GRADES, BTN_SYNC, BTN_IDEAS, BTN_SETTINGS, BTN_HELP):
        kb.button(text=text)
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def login_button() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Войти через МИРЭА", callback_data="login")
    return kb.as_markup()


def settings_keyboard(s: UserSettings) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for field, label in SETTINGS_LABELS.items():
        state = "вкл" if getattr(s, field) else "выкл"
        kb.button(text=f"{label}: {state}", callback_data=f"s:{field}")
    for h in REMINDER_CHOICES:
        mark = "[x]" if h in s.reminders else "[ ]"
        kb.button(text=f"{mark} {fmt_hours(h)}", callback_data=f"r:{h}")
    kb.button(text="Выйти и удалить данные", callback_data="logout")
    kb.adjust(*([1] * len(SETTINGS_LABELS)), 3, 3, 1)
    return kb.as_markup()


SETTINGS_TEXT = (
    "⚙️ <b>Настройки уведомлений</b>\n\n"
    "Нажми, чтобы включить или выключить. Нижние кнопки — за сколько до срока напоминать."
)
