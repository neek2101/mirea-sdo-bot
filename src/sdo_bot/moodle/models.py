"""Данные, которые бот достаёт из Moodle. Только то, что реально используется."""

from __future__ import annotations

from dataclasses import dataclass, field


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

    @property
    def is_quiz(self) -> bool:
        return self.modname == "quiz"


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
