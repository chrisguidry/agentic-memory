"""The opening list: the top of a scope's list, handed to a session's first ask.

These run against the store, because the list is one query that has to reach
the scopes above the session's, leave out what has no scope, and leave out what
the session already saw.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.recall import LIMIT, opening

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
