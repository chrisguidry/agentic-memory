"""Walking a sample and taking a person's judgment of each pair.

Everything here runs against a real database, because the query is what
proves a labelled pair is not offered again. Every session, prompt, and
statement here is invented.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from agentic_memory.app import app
from agentic_memory.labels.pairs import next_unlabelled, write_label
from agentic_memory.labels.sample import build_sample

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "github.com/acme/widget"


async def said(
    store: asyncpg.Pool,
    session_id: str,
    entry_id: str,
    occurred_at: datetime,
    body: str,
    actor: str = "human",
    actor_depth: int = 0,
    working_directory: str | None = None,
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
        " attributes, session_id, entry_id, kind, body, actor, actor_depth, working_directory)"
        " VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',"
        " $3, $4, 'prompt', $5, $6, $7, $8)",
        export_id,
        occurred_at,
        session_id,
        entry_id,
        body,
        actor,
        actor_depth,
        working_directory,
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


async def held(
    store: asyncpg.Pool,
    statement: str,
    kind: str = "semantic",
    scope_key: str | None = SCOPE,
    entry_id: str | None = None,
) -> int:
    """One live statement, and its id."""
    return await store.fetchval(
        "INSERT INTO memories (statement, kind, score, scope_key, session_id, entry_id, model,"
        " questions_fingerprint, said_at, actor, actor_depth)"
        " VALUES ($1, $2, 0.9, $3, 's1', $4, 'jev-1.13.0', 'fp', now(), 'human', 0)"
        " RETURNING id",
        statement,
        kind,
        scope_key,
        entry_id or statement,
    )


async def injected(
    store: asyncpg.Pool,
    session_id: str,
    injected_at: datetime,
    memory_ids: list[int],
    harness: str = "pi",
    scope_key: str | None = SCOPE,
) -> int:
    """One turn's handout, recorded the way `recall.record` writes it."""
    return await store.fetchval(
        "INSERT INTO injections (session_id, harness, scope_key, memory_ids, injected_at)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        session_id,
        harness,
        scope_key,
        memory_ids,
        injected_at,
    )


class TestNextUnlabelled:
    async def filed(self, store: asyncpg.Pool) -> tuple[int, int]:
        first = await held(store, "rule one")
        second = await held(store, "rule two", entry_id="e2")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await said(store, "s1", "e2", NOW + timedelta(minutes=1), "and rollbacks?")
        await injected(store, "s1", NOW, [first])
        await injected(store, "s1", NOW + timedelta(minutes=1), [second])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        return first, second

    async def test_the_first_call_gives_the_first_pair(self, store: asyncpg.Pool):
        first, _ = await self.filed(store)
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"]["memory_id"] == first
        assert (found["done"], found["total"]) == (0, 2)

    async def test_a_labelled_pair_is_not_offered_again(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        shown = await next_unlabelled(store, sample="sample")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        found = await next_unlabelled(store, sample="sample", after=shown["pair"]["id"])
        assert found["pair"]["memory_id"] == second
        assert found["done"] == 1

    async def test_every_pair_labelled_offers_nothing(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        await write_label(store, session_id="s1", entry_id="e2", memory_id=second, label="noise")
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"] is None
        assert (found["done"], found["total"]) == (2, 2)

    async def test_a_skip_wraps_back_to_what_was_skipped(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        shown = await next_unlabelled(store, sample="sample")
        skipped = await next_unlabelled(store, sample="sample", after=shown["pair"]["id"])
        assert skipped["pair"]["memory_id"] == second
        wrapped = await next_unlabelled(store, sample="sample", after=skipped["pair"]["id"])
        assert wrapped["pair"]["memory_id"] == first

    async def test_a_pair_carries_the_prompt_and_the_statement(self, store: asyncpg.Pool):
        await self.filed(store)
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"]["prompt"] == "how do we deploy?"
        assert found["pair"]["statement"] == "rule one"

    async def test_a_pair_labelled_under_another_sample_counts_as_done(self, store: asyncpg.Pool):
        first, _ = await self.filed(store)
        await build_sample(
            store, name="other", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        found = await next_unlabelled(store, sample="other")
        assert found["done"] == 1

    async def test_a_pair_carries_the_sessions_scope_directory_and_moment(
        self, store: asyncpg.Pool
    ):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?", working_directory="/repo/widget")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        pair = (await next_unlabelled(store, sample="sample"))["pair"]
        assert pair["session_scope_key"] == SCOPE
        assert pair["working_directory"] == "/repo/widget"
        assert pair["occurred_at"] == NOW.isoformat()

    async def test_a_pair_carries_the_agents_last_reply(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await replied(store, "s1", "r1", NOW - timedelta(minutes=1), "want me to roll it back too?")
        await said(store, "s1", "e1", NOW, "yeah do that")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        pair = (await next_unlabelled(store, sample="sample"))["pair"]
        assert pair["last_reply"] == "want me to roll it back too?"

    async def test_a_prompt_with_no_preceding_reply_carries_no_last_reply(
        self, store: asyncpg.Pool
    ):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        pair = (await next_unlabelled(store, sample="sample"))["pair"]
        assert pair["last_reply"] is None


class TestWriteLabel:
    async def test_a_label_can_be_written(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="good")
        assert await store.fetchval("SELECT label FROM labels") == "good"

    async def test_a_second_label_replaces_the_first(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="good")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="wrong")
        assert await store.fetchval("SELECT count(*) FROM labels") == 1
        assert await store.fetchval("SELECT label FROM labels") == "wrong"

    async def test_a_label_outside_the_three_kinds_is_refused(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        with pytest.raises(asyncpg.CheckViolationError):
            await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="meh")


@pytest.fixture
async def client(store: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    """The service, with a store of its own and nothing else running."""
    app.state.pool = store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as calling:
        yield calling


class TestRoutes:
    async def test_the_next_route_and_the_label_route_agree_with_the_functions(
        self, client: httpx.AsyncClient, store: asyncpg.Pool
    ):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )

        first = await client.get("/labels/next", params={"sample": "sample"})
        assert first.status_code == 200
        pair = first.json()["pair"]
        assert pair["statement"] == "a rule"

        posted = await client.post(
            "/labels",
            json={
                "session_id": pair["session_id"],
                "entry_id": pair["entry_id"],
                "memory_id": pair["memory_id"],
                "label": "good",
            },
        )
        assert posted.status_code == 200

        done = await client.get("/labels/next", params={"sample": "sample"})
        body = done.json()
        assert body["pair"] is None
        assert body["done"] == 1
