"""What a turn is handed, and the record of the handout.

These run against the store, because the read is one query that has to leave
out what a session already saw, and the write has to say exactly what went.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.recall import LIMIT, handed, recall

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


async def statements(store, session_id="turn-1", scope_key="github.com/liken-sh/liken", limit=10):
    found = await recall(
        store, session_id=session_id, harness="pi", scope_key=scope_key, limit=limit, now=NOW
    )
    return [row["statement"] for row in found]


class TestRecall:
    @pytest.fixture
    async def filed(self, store: asyncpg.Pool) -> asyncpg.Pool:
        await held(store, None, "everywhere")
        await held(store, "github.com/liken-sh", "the org")
        await held(store, "github.com/liken-sh/liken", "the repo")
        await held(store, "github.com/other/thing", "elsewhere")
        return store

    async def test_a_turn_is_handed_what_reaches_its_scope(self, filed):
        assert set(await statements(filed)) == {"everywhere", "the org", "the repo"}

    async def test_the_order_is_the_rank(self, store):
        await held(store, None, "a plan", kind="prospective")
        await held(store, None, "an old rule", age_in_days=400)
        await held(store, None, "a fresh rule")
        assert await statements(store) == ["a fresh rule", "an old rule", "a plan"]

    async def test_a_second_turn_of_the_same_session_is_not_handed_the_same_again(self, filed):
        first = await statements(filed)
        assert first
        assert await statements(filed) == []

    async def test_what_a_session_missed_arrives_on_its_next_turn(self, filed):
        assert len(await statements(filed, limit=2)) == 2
        assert len(await statements(filed, limit=2)) == 1
        assert await statements(filed, limit=2) == []

    async def test_a_different_session_gets_the_whole_list(self, filed):
        await statements(filed, session_id="turn-1")
        assert len(await statements(filed, session_id="turn-2")) == 3

    async def test_a_handout_is_recorded_with_what_went(self, filed):
        found = await recall(
            filed, session_id="turn-1", harness="pi", scope_key="github.com/liken-sh", limit=10
        )
        [record] = await handed(filed, session_id="turn-1")
        assert record["memory_ids"] == [row["id"] for row in found]
        assert record["harness"] == "pi"
        assert record["scope_key"] == "github.com/liken-sh"

    async def test_nothing_is_recorded_when_nothing_went(self, filed):
        await statements(filed, scope_key="codeberg.org/nobody/nothing")
        assert len(await handed(filed, session_id="turn-1")) == 1
        await statements(filed, scope_key="codeberg.org/nobody/nothing")
        assert len(await handed(filed, session_id="turn-1")) == 1

    async def test_no_scope_reaches_only_what_holds_everywhere(self, filed):
        assert await statements(filed, scope_key=None) == ["everywhere"]

    async def test_the_limit_is_capped(self, store):
        for n in range(LIMIT + 5):
            await held(store, None, f"rule {n}")
        assert len(await statements(store, limit=LIMIT + 5)) == LIMIT

    async def test_a_retired_statement_is_not_handed_out(self, store):
        old = await held(store, None, "the old way")
        new = await held(store, None, "the new way")
        await store.execute(
            "UPDATE memories SET superseded_by = $2, superseded_at = now() WHERE id = $1", old, new
        )
        assert await statements(store) == ["the new way"]
