"""Высокоуровневые запросы к СДО: что именно и каким вызовом Moodle мы получаем.

Все AJAX-функции ниже помечены в Moodle 4.1–4.5 как 'ajax' => true
(lib/db/services.php, message/output/popup/db/services.php).
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .client import MoodleClient, MoodleError, SessionExpired
from .models import Activity, Course, CourseGrade, GradeItem, Module, Notification, SiteInfo
from .parsers import (
    Participant,
    parse_group_options,
    parse_overview,
    parse_participants,
    parse_profile,
    parse_user_report,
)

EVENTS_PAGE = 50
# Модули, у которых нет своей страницы (текст прямо на странице курса) — не «материалы»
SKIP_MODULES = frozenset({"label", "subsection"})


def strip_html(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class MoodleAPI:
    def __init__(self, client: MoodleClient):
        self.client = client
        self.base_url = client.base_url

    async def site_info(self) -> SiteInfo:
        # core_webservice_get_site_info не помечена 'ajax' => true — СДО отвечает
        # «Веб-служба недоступна», поэтому берём id и ФИО со страницы профиля
        user_id, full_name, site_name = parse_profile(await self.client.get_page("/user/profile.php"))
        if not user_id:
            raise SessionExpired("СДО открыла профиль гостя: вход не выполнен")
        return SiteInfo(user_id=user_id, full_name=full_name, site_name=site_name)

    async def courses(self) -> list[Course]:
        d = await self.client.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {"classification": "inprogress", "limit": 0, "offset": 0, "sort": "fullname"},
        )
        return [
            Course(
                int(c["id"]),
                html.unescape(c.get("fullname") or c.get("shortname") or str(c["id"])),
                c.get("viewurl") or f"{self.base_url}/course/view.php?id={c['id']}",
            )
            for c in d.get("courses", [])
        ]

    async def activities(self, since_ts: int) -> list[Activity]:
        """Задания и тесты, по которым ещё нужно действие (сданное Moodle сюда не отдаёт)."""
        events: list[dict[str, Any]] = []
        args: dict[str, Any] = {"timesortfrom": since_ts, "limitnum": EVENTS_PAGE}
        for _ in range(20):  # предохранитель от бесконечной пагинации
            d = await self.client.call("core_calendar_get_action_events_by_timesort", args)
            page = d.get("events") or []
            events += page
            if len(page) < EVENTS_PAGE or not d.get("lastid"):
                break
            args = {**args, "aftereventid": d["lastid"]}
        return merge_events(events, self.base_url)

    async def course_modules(self, course_ids: list[int]) -> dict[int, list[Module] | MoodleError]:
        # core_course_get_contents через AJAX не вызвать («Веб-служба недоступна»). Берём то же,
        # что рисует страница курса в Moodle 4.x: состояние формата курса (JSON-строка)
        calls = [("core_courseformat_get_state", {"courseid": cid}) for cid in course_ids]
        results = await self.client.batch(calls) if calls else []
        out: dict[int, list[Module] | MoodleError] = {}
        for cid, res in zip(course_ids, results, strict=True):
            if isinstance(res, MoodleError):
                out[cid] = res
            else:
                out[cid] = parse_course_state(cid, json.loads(res) if isinstance(res, str) else res or {})
        return out

    async def grade_overview(self) -> list[CourseGrade]:
        page = await self.client.get_page("/grade/report/overview/index.php")
        return parse_overview(page, self.base_url)

    async def course_grade_items(self, course_id: int) -> list[GradeItem]:
        page = await self.client.get_page("/grade/report/user/index.php", {"id": course_id})
        return parse_user_report(page)

    async def my_group(self, course_ids: list[int], tries: int = 5) -> str | None:
        """Учебная группа студента. Функции групп через AJAX закрыты, поэтому смотрим фильтр
        на странице участников: в курсах с раздельными группами там видна только своя группа."""
        seen: dict[str, int] = {}
        for cid in course_ids[:tries]:
            page = await self.client.get_page("/user/index.php", {"id": cid, "perpage": 1})
            groups = {name for _, name in parse_group_options(page)}
            if len(groups) == 1:
                name = groups.pop()
                seen[name] = seen.get(name, 0) + 1
                if seen[name] >= 2 or len(course_ids) == 1:
                    return name
        return max(seen, key=lambda k: seen[k]) if seen else None

    async def group_roster(self, course_ids: list[int], group_name: str, tries: int = 5) -> list[Participant]:
        """Студенты группы по странице участников первого курса, где эта группа есть в фильтре."""
        for cid in course_ids[:tries]:
            page = await self.client.get_page("/user/index.php", {"id": cid, "perpage": 1})
            gid = next((g for g, name in parse_group_options(page) if name == group_name), None)
            if gid is None:
                continue
            page = await self.client.get_page("/user/index.php", {"id": cid, "group": gid, "perpage": 300})
            people = [p for p in parse_participants(page) if p.group in ("", group_name)]
            if people:
                return sorted(people, key=lambda p: p.full_name.casefold())
        return []

    async def notifications(self, user_id: int, limit: int = 20) -> list[Notification]:
        d = await self.client.call(
            "message_popup_get_popup_notifications",
            {"useridto": user_id, "newestfirst": True, "limit": limit, "offset": 0},
        )
        return [
            Notification(
                id=int(n["id"]),
                subject=strip_html(n.get("subject"), 200),
                text=strip_html(n.get("smallmessage") or n.get("fullmessage")),
                url=n.get("contexturl") or "",
                created_ts=int(n.get("timecreated") or 0),
                component=n.get("component") or "",
            )
            for n in d.get("notifications", [])
        ]


def merge_events(events: list[dict[str, Any]], base_url: str) -> list[Activity]:
    """Склеивает события календаря в задания: у теста «открытие» и «закрытие» — одно целое."""
    acts: dict[str, Activity] = {}
    for e in events:
        mod = e.get("modulename") or (e.get("component") or "").removeprefix("mod_") or "event"
        instance = e.get("instance")
        key = f"{mod}:{instance}" if instance else f"event:{e['id']}"
        course = e.get("course") or {}
        a = acts.get(key)
        if a is None:
            a = acts[key] = Activity(
                key=key,
                modname=mod,
                name=html.unescape(e.get("activityname") or e.get("name") or "Без названия").strip(),
                course_id=int(course.get("id") or e.get("courseid") or 0),
                course_name=html.unescape(course.get("fullname") or ""),
                url=e.get("url") or e.get("viewurl") or f"{base_url}/calendar/view.php?view=upcoming",
            )
        ts = int(e["timesort"])
        etype = e.get("eventtype")
        if etype == "open":
            a.open_ts = ts
        elif etype in ("close", "due"):
            a.close_ts = ts
        elif a.close_ts is None:
            a.close_ts = ts  # прочие типы (ожидаемое выполнение и т.п.) — только если срока нет
    return sorted(acts.values(), key=lambda a: (a.close_ts or a.open_ts or 0, a.name))


def parse_course_state(course_id: int, state: dict[str, Any]) -> list[Module]:
    """Ответ core_courseformat_get_state: section[] (title, cmlist) и cm[].

    В cm поле module — тип («quiz»), а modname — его название на языке сайта («Тест»).
    """
    titles = {str(s["id"]): html.unescape(s.get("title") or "") for s in state.get("section") or []}
    modules = []
    for m in state.get("cm") or []:
        kind = m.get("module") or ""
        if kind in SKIP_MODULES or not m.get("uservisible", True) or not m.get("url"):
            continue
        modules.append(
            Module(
                cmid=int(m["id"]),
                course_id=course_id,
                name=html.unescape(m.get("name") or "").strip(),
                modname=kind,
                modplural=m.get("modname") or kind,
                url=m["url"],
                section=titles.get(str(m.get("sectionid")), ""),
            )
        )
    return modules
