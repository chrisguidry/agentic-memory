"""The exchanges before a pair's prompt.

Everything here runs against a real database, because the query is what
proves an exchange pages back one real prompt at a time and stops its
replies at the next one. Every session, prompt, and reply here is invented.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from agentic_memory.app import app
from agentic_memory.labels.context import earlier_exchange

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


async def said(
    store: asyncpg.Pool, session_id: str, entry_id: str, occurred_at: datetime, body: str
) -> None:
    """One prompt, in the record, at a moment."""
    await store.execute(
        "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') ON CONFLICT DO NOTHING"
    )
    await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
    export_id = await store.fetchval(
        "INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)"
        " VALUES ($1, $2, (SELECT id FROM resources), (SELECT id FROM scopes), '{}')"
        " RETURNING id",
        session_id,
        entry_id,
    )
    await store.execute(
        "INSERT INTO logs (export_id, received_at, resource_id, scope_id, occurred_at,"
        " attributes, session_id, entry_id, kind, body, actor, actor_depth)"
        " VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',"
        " $3, $4, 'prompt', $5, 'human', 0)",
        export_id,
        occurred_at,
        session_id,
        entry_id,
        body,
    )


async def replied(
    store: asyncpg.Pool, session_id: str, entry_id: str, occurred_at: datetime, body: str
) -> None:
    """One of the agent's own replies, in the record, at a moment."""
    await store.execute(
        "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') ON CONFLICT DO NOTHING"
    )
    await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
    export_id = await store.fetchval(
        "INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)"
        " VALUES ($1, $2, (SELECT id FROM resources), (SELECT id FROM scopes), '{}')"
        " RETURNING id",
        session_id,
        entry_id,
    )
    await store.execute(
        "INSERT INTO logs (export_id, received_at, resource_id, scope_id, occurred_at,"
        " attributes, session_id, entry_id, kind, body, actor, actor_depth)"
        " VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',"
        " $3, $4, 'response', $5, 'agent', 0)",
        export_id,
        occurred_at,
        session_id,
        entry_id,
        body,
    )


class TestEarlierExchange:
    async def test_the_first_page_is_the_exchange_right_before_the_prompt(
        self, store: asyncpg.Pool
    ):
        await said(store, "s1", "e1", NOW - timedelta(minutes=10), "first question")
        await replied(store, "s1", "r1", NOW - timedelta(minutes=9), "first answer")
        await said(store, "s1", "e2", NOW, "the real question")
        found = await earlier_exchange(store, session_id="s1", entry_id="e2", page=0)
        assert found["entry_id"] == "e1"
        assert found["prompt"] == "first question"
        assert found["replies"] == ["first answer"]

    async def test_a_later_page_reaches_further_back(self, store: asyncpg.Pool):
        await said(store, "s1", "e1", NOW - timedelta(minutes=20), "older question")
        await said(store, "s1", "e2", NOW - timedelta(minutes=10), "newer question")
        await said(store, "s1", "e3", NOW, "the real question")
        first = await earlier_exchange(store, session_id="s1", entry_id="e3", page=0)
        second = await earlier_exchange(store, session_id="s1", entry_id="e3", page=1)
        assert (first["entry_id"], second["entry_id"]) == ("e2", "e1")

    async def test_replies_are_bounded_by_the_next_prompt(self, store: asyncpg.Pool):
        await said(store, "s1", "e1", NOW - timedelta(minutes=20), "older question")
        await replied(store, "s1", "r1", NOW - timedelta(minutes=19), "an old answer")
        await said(store, "s1", "e2", NOW - timedelta(minutes=10), "newer question")
        await replied(store, "s1", "r2", NOW - timedelta(minutes=9), "a newer answer")
        await said(store, "s1", "e3", NOW, "the real question")
        found = await earlier_exchange(store, session_id="s1", entry_id="e3", page=1)
        assert found["entry_id"] == "e1"
        assert found["replies"] == ["an old answer"]

    async def test_a_plumbing_prompt_is_skipped(self, store: asyncpg.Pool):
        await said(
            store, "s1", "e1", NOW - timedelta(minutes=10), "<command-name>/clear</command-name>"
        )
        await said(store, "s1", "e2", NOW - timedelta(minutes=5), "real earlier question")
        await said(store, "s1", "e3", NOW, "the real question")
        found = await earlier_exchange(store, session_id="s1", entry_id="e3", page=0)
        assert found["entry_id"] == "e2"

    async def test_paging_past_the_sessions_start_gives_nothing(self, store: asyncpg.Pool):
        await said(store, "s1", "e1", NOW - timedelta(minutes=10), "only earlier question")
        await said(store, "s1", "e2", NOW, "the real question")
        found = await earlier_exchange(store, session_id="s1", entry_id="e2", page=1)
        assert found is None

    async def test_a_prompt_with_no_earlier_exchange_gives_nothing(self, store: asyncpg.Pool):
        await said(store, "s1", "e1", NOW, "the only question")
        found = await earlier_exchange(store, session_id="s1", entry_id="e1", page=0)
        assert found is None


@pytest.fixture
async def client(store: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    """The service, with a store of its own and nothing else running."""
    app.state.pool = store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as calling:
        yield calling


class TestRoutes:
    async def test_the_context_route_agrees_with_the_function(
        self, client: httpx.AsyncClient, store: asyncpg.Pool
    ):
        await said(store, "s1", "e1", NOW - timedelta(minutes=10), "an earlier question")
        await said(store, "s1", "e2", NOW, "the real question")

        found = await client.get(
            "/labels/context", params={"session_id": "s1", "entry_id": "e2", "page": 0}
        )
        assert found.status_code == 200
        exchange = found.json()["exchange"]
        assert exchange["prompt"] == "an earlier question"

        past = await client.get(
            "/labels/context", params={"session_id": "s1", "entry_id": "e2", "page": 1}
        )
        assert past.json()["exchange"] is None
