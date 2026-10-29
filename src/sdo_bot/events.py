"""События, о которых бот сообщает пользователю. Отделены от форматирования и отправки."""

from __future__ import annotations

from dataclasses import dataclass, field

from .moodle.models import Activity, Course, Module, Notification


@dataclass(frozen=True)
class Welcome:
    """Первая сводка после входа: что уже идёт и что впереди."""

    active: list[Activity]
    upcoming: list[Activity]
    overdue: list[Activity]
    courses: int
    full_name: str = ""


@dataclass(frozen=True)
class NewActivity:
    activity: Activity


@dataclass(frozen=True)
class DeadlineChanged:
    activity: Activity
    old_close_ts: int | None


@dataclass(frozen=True)
class QuizOpened:
    activity: Activity


@dataclass(frozen=True)
class Reminder:
    activity: Activity
    hours: int  # порог, который сработал


@dataclass(frozen=True)
class NewCourse:
    course: Course


@dataclass(frozen=True)
class NewMaterials:
    course: Course
    modules: list[Module] = field(default_factory=list)


@dataclass(frozen=True)
class GradeChanged:
    course_id: int
    course_name: str
    item_name: str | None  # None — известен только итог курса
    old: str | None
    new: str
    range: str = ""
    total: str | None = None


@dataclass(frozen=True)
class MoodleNotice:
    notification: Notification


Event = (
    Welcome
    | NewActivity
    | DeadlineChanged
    | QuizOpened
    | Reminder
    | NewCourse
    | NewMaterials
    | GradeChanged
    | MoodleNotice
)
