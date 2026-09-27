"""The sort on the writer's path, where a model that fails is tried again.

The worker writes a message's statements, embeds them, sorts the prospective
ones, and merges them. A task that fails is run again, and the writer writes
nothing the second time, so every step after it has to run on every attempt.
"""

import json
from types import SimpleNamespace

import asyncpg
import pytest
from _sorting import FakeJudge

from agentic_memory.classify import KIND_COLUMNS, questions_fingerprint
from agentic_memory.embed import Embedder
from agentic_memory.memories import memories
from agentic_memory.settings import Settings
from agentic_memory.synthesize import synthesize


class Writer:
    """The model that writes the sentences, replying with a fact and a plan."""

    async def post(self, url, *, headers, json):
        reply = (
            '[{"kind": "semantic", "statement": "The widget is written in Go."},'
            ' {"kind": "prospective",'
            ' "statement": "The widget will store its state in Postgres."}]'
        )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": reply}}]},
        )


class FailingJudge(FakeJudge):
    """A model that fails its first `failures` calls the way a timeout or a 503 does."""

    def __init__(self, failures: int, **answers):
        super().__init__(**answers)
        self.failures = failures

    async def system_one(self, *, state, questions):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the provider answered 503")
        return await super().system_one(state=state, questions=questions)


@pytest.fixture
async def classified(store: asyncpg.Pool) -> asyncpg.Pool:
    """A message the classifier found a fact and a plan in."""
    scores = dict.fromkeys(KIND_COLUMNS, 0.05) | {"prospective": 0.95, "semantic": 0.95}
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, model, questions_fingerprint, rounds, state,
             {", ".join(scores)})
        VALUES ('s1', 'e1', 'jev-1.13.0', $1, 5, $2::jsonb,
                {", ".join(f"${number}" for number in range(3, 3 + len(scores)))})
        """,
        questions_fingerprint(),
        json.dumps({"before": "", "message": "it's Go, and Postgres is next"}),
        *scores.values(),
    )
    return store


async def written(store: asyncpg.Pool, embedder: Embedder, judge) -> None:
    await synthesize(
        "s1",
        "e1",
        settings=Settings(),
        pool=store,
        client=Writer(),
        embedder=embedder,
        judge=judge,
    )


async def live(store: asyncpg.Pool) -> list[dict]:
    rows = await store.fetch(
        "SELECT statement, kind, embedding IS NOT NULL AS embedded FROM memories"
        " WHERE superseded_by IS NULL AND ended_at IS NULL ORDER BY id"
    )
    return [dict(row) for row in rows]


async def test_a_statement_is_sorted_when_it_is_written(classified, embedder: Embedder):
    await written(classified, embedder, FakeJudge(decision=0.9, kind="procedural"))
    kinds = {row["statement"]: row["kind"] for row in await memories(classified)}
    assert kinds == {
        "The widget is written in Go.": "semantic",
        "The widget will store its state in Postgres.": "procedural",
    }


async def test_a_retry_after_a_failed_sort_embeds_every_statement(classified, embedder: Embedder):
    judge = FailingJudge(1, decision=0.9, kind="procedural")
    with pytest.raises(RuntimeError):
        await written(classified, embedder, judge)
    await written(classified, embedder, judge)
    assert await live(classified) == [
        {"statement": "The widget is written in Go.", "kind": "semantic", "embedded": True},
        {
            "statement": "The widget will store its state in Postgres.",
            "kind": "procedural",
            "embedded": True,
        },
    ]


async def test_a_sort_that_never_succeeds_leaves_nothing_unembedded(classified, embedder: Embedder):
    judge = FailingJudge(4, decision=0.9, kind="procedural")
    for _ in range(4):
        with pytest.raises(RuntimeError):
            await written(classified, embedder, judge)
    assert [row["embedded"] for row in await live(classified)] == [True, True]
