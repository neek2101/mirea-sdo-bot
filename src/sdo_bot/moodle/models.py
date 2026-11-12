"""Данные, которые бот достаёт из Moodle. Только то, что реально используется."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PART = re.compile(r"\(\s*ч(?:асть|\.)\s*[\d/ ]*\)", re.I)  # «(часть 1/2)», «(часть 2)»
_SEMESTER = re.compile(r"\[[^\]]*\]")  # «[I.26-27]»
_PARENS = re.compile(r"\(([^()]*)\)")


def clean_course_name(name: str) -> str:
    """«Программирование_Зачет (часть 1/2) [I.26-27]» → «Программирование — Зачет».

    Служебные «(часть …)» и «[семестр]» убираем целиком, у прочих скобок оставляем
    только содержимое, подчёркивание СДО ставит между предметом и формой контроля.
    """
    s = _SEMESTER.sub(" ", _PART.sub(" ", name))
    s = _PARENS.sub(r" \1 ", s)
    s = re.sub(r"\s*_+\s*", " — ", s)
    s = re.sub(r"\s+", " ", s).strip(" —")
    return s or name.strip()


def clean_activity_name(name: str) -> str:
    """Подчёркивания в названиях работ («отчет на проверку_ Иванова») — пробелы."""
    s = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    return s or name.strip()


def max_points(grade_range: str) -> str:
    """Максимум из столбца «Диапазон» отчёта об оценках: «0–10» → «10», «0,00–2,50» → «2,5»."""
    m = re.search(r"[–—-]\s*(\d+(?:[.,]\d+)?)\s*$", grade_range or "")
    if not m:
        return ""  # шкала («Зачтено»/«Отлично») или пусто
    value = m.group(1).replace(".", ",")
    if "," in value:
        value = value.rstrip("0").rstrip(",")
    return value


@dataclass(frozen=True)
class Course:
    id: int
    name: str
    url: str


@dataclass
class Activity:
    """Задание или тест, по которому студенту ещё нужно что-то сделать.

    Склеено из событий календаря: у теста бывает два события (открытие и закрытие),
    у задания — одно (срок сдачи).
    """

    key: str  # "quiz:123" — модуль и id экземпляра, стабилен между опросами
    modname: str
    name: str
    course_id: int
    course_name: str
    url: str
    open_ts: int | None = None
    close_ts: int | None = None
    # Только у заданий старосты (modname "group"): кто поставил, для какой группы, пояснение
    author: str = ""
    group: str = ""
    note: str = ""
    # Максимум баллов из журнала оценок: None — ещё не смотрели, "" — в журнале нет
    max_grade: str | None = None
    # Можно ли уже начать тест/сдать задание: action.actionable события календаря СДО.
    # False у работ с ограничением доступа и у заданий, где приём ответов ещё не начался
    available: bool = True

    def locked(self, now: int) -> bool:
        """Срок в календаре есть, а работа ещё закрыта, и время открытия СДО не сообщила."""
        return not self.available and not (self.open_ts and self.open_ts > now)

    @property
    def cmid(self) -> int | None:
        m = re.search(r"/mod/\w+/view\.php\?(?:.*&)?id=(\d+)", self.url)
        return int(m.group(1)) if m else None

    @property
    def is_quiz(self) -> bool:
        return self.modname == "quiz"

    @property
    def by_starosta(self) -> bool:
        return self.modname == "group"


@dataclass(frozen=True)
class Module:
    """Элемент курса: файл, страница, тест, задание, ссылка…"""

    cmid: int
    course_id: int
    name: str
    modname: str
    modplural: str
    url: str
    section: str


@dataclass(frozen=True)
class GradeItem:
    key: str  # стабильный ключ строки отчёта
    name: str
    grade: str  # как показывает Moodle: «8,00», «Отлично», «-»
    range: str = ""
    percentage: str = ""
    is_total: bool = False
    cmid: int | None = None  # элемент курса, если строка ссылается на тест/задание

    @property
    def has_grade(self) -> bool:
        return self.grade not in ("", "-", "–", "—")


@dataclass(frozen=True)
class CourseGrade:
    course_id: int
    course_name: str
    grade: str
    url: str = ""


@dataclass(frozen=True)
class Notification:
    id: int
    subject: str
    text: str
    url: str
    created_ts: int
    component: str = ""


@dataclass
class SiteInfo:
    user_id: int
    full_name: str
    site_name: str = ""
    release: str = ""
    extra: dict = field(default_factory=dict)
