"""The opening list: the top of a scope's list, handed to a session's first ask.

These run against the store, because the list is one query that has to reach
the scopes above the session's, leave out what has no scope, and leave out what
the session already saw. A turn that records a handout runs with the real
model, because what it records depends on what the match found.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from pydantic import ValidationError

from agentic_memory.embed import Embedder, embed_missing
from agentic_memory.metrics import REGISTRY
from agentic_memory.recall import LIMIT, opening, seen_by, turn
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


async def held(
    store: asyncpg.Pool,
    scope_key: str | None,
    statement: str,
    kind: str = "preference",
    age_in_days: float = 0.0,
) -> int:
    """One live statement in the store, and its id."""
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, actor, actor_depth)
        VALUES ($1, $2, 0.9, $3, 's1', $1, 'jev-1.13.0', 'fp', $4, 'human', 0)
        RETURNING id
        """,
        statement,
        kind,
        scope_key,
        NOW - timedelta(days=age_in_days),
    )


async def statements(store, scope_key="github.com/liken-sh/liken", seen=(), limit=10):
    found = await opening(store, scope_key=scope_key, seen=seen, limit=limit, now=NOW)
    return [row["statement"] for row in found]


class TestOpening:
    @pytest.fixture
    async def filed(self, store: asyncpg.Pool) -> asyncpg.Pool:
        await held(store, None, "everywhere")
        await held(store, "github.com/liken-sh", "the org")
        await held(store, "github.com/liken-sh/liken", "the repo")
        await held(store, "github.com/other/thing", "elsewhere")
        return store

    async def test_the_list_holds_what_reaches_the_scope(self, filed):
        assert set(await statements(filed)) == {"the org", "the repo"}

    async def test_the_order_is_the_rank(self, store):
        await held(store, "github.com/liken-sh", "a plan", kind="prospective")
        await held(store, "github.com/liken-sh", "an old rule", age_in_days=400)
        await held(store, "github.com/liken-sh", "a fresh rule")
        assert await statements(store) == ["a fresh rule", "an old rule", "a plan"]

    async def test_what_the_session_saw_is_left_off(self, filed):
        org = await filed.fetchval("SELECT id FROM memories WHERE statement = 'the org'")
        assert await statements(filed, seen={org}) == ["the repo"]

    async def test_a_statement_with_no_scope_is_left_off_the_list(self, filed):
        # A statement that holds everywhere reaches a session through the match,
        # which reads the prompt, and the list is only for where the session is.
        assert "everywhere" not in await statements(filed)

    async def test_a_session_with_no_scope_has_no_list(self, filed):
        assert await statements(filed, scope_key=None) == []

    async def test_the_limit_is_capped(self, store):
        for n in range(LIMIT + 5):
            await held(store, "github.com/liken-sh", f"rule {n}")
        assert len(await statements(store, limit=LIMIT + 5)) == LIMIT

    async def test_a_retired_statement_is_left_off(self, store):
        old = await held(store, "github.com/liken-sh", "the old way")
        new = await held(store, "github.com/liken-sh", "the new way")
        await store.execute(
            "UPDATE memories SET superseded_by = $2, superseded_at = now() WHERE id = $1", old, new
        )
        assert await statements(store) == ["the new way"]


@pytest.fixture
async def embedded(store: asyncpg.Pool, embedder: Embedder) -> asyncpg.Pool:
    """Enough embedded statements in one scope for a match to have a baseline."""
    for n in range(12):
        await held(store, "github.com/liken-sh/liken", f"Rule {n} of the widget's tests.")
    await embed_missing(store, embedder)
    return store


# No margin, so the match finds more than the prompt limit and the list has
# statements left over.
OPEN_HANDED = Settings(recall_margin=0.0, recall_prompt_limit=5, recall_opening_limit=3)


async def first_turn(store: asyncpg.Pool, embedder: Embedder):
    return await turn(
        store,
        embedder,
        OPEN_HANDED,
        session_id="s1",
        harness="pi",
        scope_key="github.com/liken-sh/liken",
        prompt="which rules do the widget's tests follow?",
        now=NOW,
    )


async def test_a_first_turn_records_what_it_matched_apart_from_what_it_listed(embedded, embedder):
    await first_turn(embedded, embedder)
    rows = await embedded.fetch(
        "SELECT form, cardinality(memory_ids) AS handed FROM injections ORDER BY form"
    )
    assert [(row["form"], row["handed"]) for row in rows] == [("match", 5), ("opening", 3)]


async def test_what_a_session_saw_is_every_form_it_was_handed(embedded, embedder):
    handout = await first_turn(embedded, embedder)
    assert await seen_by(embedded, "s1") == {row["id"] for row in handout.statements}


def lists_read() -> float:
    """How many times a recall has read a scope's list, as the metrics count it."""
    return (
        REGISTRY.get_sample_value("agentic_memory_recall_seconds_count", {"phase": "opening"}) or 0
    )


# With the list off, a session that was handed nothing keeps the opening form on
# every turn, so a read of the list on that form would run on every one of them.
@pytest.mark.parametrize(("limit", "reads"), [(0, 0), (3, 1)], ids=["off", "on"])
async def test_a_turn_reads_the_list_only_when_it_is_on(store, embedder, limit, reads):
    before = lists_read()
    await turn(
        store,
        embedder,
        Settings(recall_opening_limit=limit),
        session_id="s1",
        harness="pi",
        scope_key="github.com/liken-sh/liken",
        prompt="",
        now=NOW,
    )
    assert lists_read() - before == reads


def test_the_list_takes_no_negative_limit():
    with pytest.raises(ValidationError, match="recall_opening_limit"):
        Settings(recall_opening_limit=-1)
