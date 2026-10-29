from __future__ import annotations

import pytest

from sdo_bot.bot.ideas import check_text, normalize
from sdo_bot.storage import Storage

pytestmark = pytest.mark.anyio


async def test_ideas_sorted_by_rating_and_votes_toggle(storage: Storage):
    a = await storage.add_idea(1, "Идея А", normalize("Идея А"))
    b = await storage.add_idea(2, "Идея Б", normalize("Идея Б"))
    c = await storage.add_idea(3, "Идея В", normalize("Идея В"))
    for voter in (10, 11):
        await storage.vote_idea(b, voter, 1)
    await storage.vote_idea(a, 10, -1)
    ideas, total = await storage.ideas_page(viewer=10, offset=0, limit=10)
    assert total == 3
    assert [(i.id, i.score, i.my_vote) for i in ideas] == [(b, 2, 1), (c, 0, 0), (a, -1, -1)]

    assert await storage.vote_idea(b, 10, 1) == 0  # повторный +1 снимает голос
    assert await storage.vote_idea(b, 10, -1) == -1  # смена голоса
    assert await storage.vote_idea(999, 10, 1) is None
    ideas, _ = await storage.ideas_page(viewer=10, offset=0, limit=1)
    assert [(i.id, i.up, i.down) for i in ideas] == [(c, 0, 0)]


async def test_deleting_user_keeps_ideas_but_removes_author_and_votes(storage: Storage):
    await storage.ensure_user(1)
    idea = await storage.add_idea(1, "Идея А", "идея а")
    await storage.vote_idea(idea, 1, 1)
    await storage.delete_user(1)
    assert await storage.idea_author(idea) == (True, None)
    ideas, _ = await storage.ideas_page(viewer=2, offset=0, limit=10)
    assert ideas[0].up == 0


def test_spam_filters():
    assert check_text("Добавить тёмную тему") is None
    assert check_text("ааа") is not None
    assert check_text("x" * 501) is not None
    assert check_text("Пишите в @spammer_channel") is not None
    assert check_text("Заходи на casino.xyz скорее") is not None
    assert check_text("1234567890 1234") is not None  # нет слов
    assert check_text("ыыыыыыыыыыыыыыыы ыы") is not None
    assert normalize("Тёмная  тема!!!") == normalize("тёмная тема")
