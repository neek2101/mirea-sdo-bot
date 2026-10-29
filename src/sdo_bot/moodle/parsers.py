"""Разбор HTML-отчётов об оценках Moodle.

Отчёты gradereport_* в Moodle не доступны через AJAX, поэтому читаем страницы:
- /grade/report/overview/index.php — итог по каждому курсу (table#overview-grade);
- /grade/report/user/index.php?id=<курс> — оценки за элементы курса (table.user-grade).

Разметка взята из исходников Moodle 4.x (grade/report/overview/lib.php,
grade/report/user/classes/report/user.php); для 3.x оставлены запасные варианты.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .models import CourseGrade, GradeItem

# Скрытый текст для скринридеров, иконки и меню действий не должны попадать в значения
_NOISE = ".sr-only, .accesshide, .visually-hidden, .action-menu, .dropdown, script, style, img, i.icon"
_TYPE_LABEL = "span.text-uppercase.small, span.d-block.text-uppercase"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    node = _clone(node)
    for junk in node.select(_NOISE):
        junk.decompose()
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _clone(node: Tag) -> Tag:
    # decompose() портит исходное дерево, поэтому чистим глубокую копию
    return copy.copy(node)


def _query_int(href: str, name: str) -> int | None:
    values = parse_qs(urlsplit(href).query).get(name)
    try:
        return int(values[0]) if values else None
    except ValueError:
        return None


def parse_overview(html: str, base_url: str) -> list[CourseGrade]:
    soup = _soup(html)
    table = soup.find("table", id="overview-grade")
    if table is None:
        return []
    result = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        link = cells[0].find("a", href=True)
        if link is None:  # пустые строки-заполнители flexible_table
            continue
        href = urljoin(base_url + "/", link["href"])
        course_id = _query_int(href, "id")
        if course_id is None:
            continue
        result.append(CourseGrade(course_id, _text(link) or _text(cells[0]), _text(cells[1]), href))
    return result


def parse_user_report(html: str) -> list[GradeItem]:
    soup = _soup(html)
    table = soup.select_one("table.user-grade")
    if table is None:
        return []
    items: list[GradeItem] = []
    seen: set[str] = set()
    for tr in table.find_all("tr"):
        name_cell = tr.select_one(".column-itemname")
        grade_cell = tr.select_one(".column-grade")
        if name_cell is None or grade_cell is None:
            continue  # заголовки категорий и шапка таблицы
        name = _item_name(name_cell)
        if not name:
            continue
        link = name_cell.find("a", href=True)
        key = name_cell.get("id") or (link["href"] if link else "") or name
        if key in seen:
            key = f"{key}#{len(items)}"
        seen.add(key)
        is_total = bool(name_cell.select_one(".courseitem") or "courseitem" in (name_cell.get("class") or []))
        items.append(
            GradeItem(
                key=str(key),
                name=name,
                grade=_text(grade_cell),
                range=_text(tr.select_one(".column-range")),
                percentage=_text(tr.select_one(".column-percentage")),
                is_total=is_total,
            )
        )
    return items


def _item_name(cell: Tag) -> str:
    title = cell.select_one(".rowtitle")  # Moodle 4.x
    if title is not None:
        return _text(title)
    cell = _clone(cell)
    for label in cell.select(_TYPE_LABEL):
        label.decompose()
    header = cell.select_one(".gradeitemheader")  # Moodle 3.x
    return _text(header) if header is not None else _text(cell)


def parse_profile(html: str) -> tuple[int, str, str]:
    """Свой профиль (/user/profile.php): id из M.cfg.userId, ФИО из заголовка, имя сайта из <title>.

    core_webservice_get_site_info через AJAX не вызвать, поэтому читаем страницу. id 0 — гость.
    """
    m = re.search(r'"userId"\s*:\s*(\d+)', html)
    user_id = int(m.group(1)) if m else 0
    soup = _soup(html)
    title = _text(soup.title)
    name = _text(soup.select_one(".page-header-headings h1") or soup.find("h1"))
    if not name:
        name = title.split(":")[0].strip()
    site = title.rsplit("|", 1)[1].strip() if "|" in title else ""
    return user_id, name, site


# Учебная группа МИРЭА: «УИБО-09-25», «БАСО-01-25»…
GROUP_RE = re.compile(r"^[А-ЯЁ]{2,6}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Participant:
    moodle_id: int
    full_name: str
    group: str


def parse_group_options(html: str) -> list[tuple[int, str]]:
    """Группы из фильтра на странице участников курса (/user/index.php): (id группы курса, название).

    В курсах с раздельными группами студент видит в фильтре только свою группу.
    """
    out = []
    for opt in _soup(html).select("option"):
        name = _text(opt)
        value = str(opt.get("value") or "")
        if GROUP_RE.match(name) and value.isdigit():
            out.append((int(value), name))
    return out


def parse_participants(html: str) -> list[Participant]:
    """Таблица участников курса: ссылка на профиль даёт id и ФИО, столбец группы — группу."""
    people = []
    for row in _soup(html).select("table#participants tbody tr"):
        if "emptyrow" in (row.get("class") or []):
            continue
        a = row.select_one("a[href*='/user/view.php']")
        uid = _query_int(str(a.get("href") or ""), "id") if a else None
        if a is None or uid is None:
            continue
        for initials in a.select(".userinitials, img"):
            initials.decompose()
        cells = [_text(td) for td in row.find_all("td")]
        group = next((c for c in cells if GROUP_RE.match(c)), "")
        people.append(Participant(uid, _text(a), group))
    return people
