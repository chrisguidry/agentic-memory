"""A statement that ends on its own, with no statement to replace it.

A statement ends when a message meets its condition, or when a re-read leaves
it behind, and a commitment with a moment ends when the moment passes. Every
read treats an ended statement the way it treats a replaced one. These run
against the store, because each read is its own query and each one has to
leave the statement out.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.db import SCHEMA, metrics
from agentic_memory.embed import Embedder, embed_missing, literal
from agentic_memory.match import match
from agentic_memory.memories import end, memories, retire, standing
from agentic_memory.merge import merge
from agentic_memory.recall import opening
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "example.test/acme/widget"
COMMITMENT = "The widget release is held until the security review passes."


async def held(
    store: asyncpg.Pool,
    statement: str = COMMITMENT,
    *,
    kind: str = "prospective",
    until_moment: datetime | None = None,
    said_at: datetime = NOW - timedelta(days=1),
) -> int:
    """One live statement in the store, and its id."""
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, until_moment, until_event, actor, actor_depth)
        VALUES ($1, $2, 0.9, $3, 's0', $1, 'jev-1.13.0', 'fp', $4, $5,
                'the security review passes', 'person', 0)
        RETURNING id
        """,
        statement,
        kind,
        SCOPE,
        said_at,
        until_moment,
    )


class TestEnd:
    async def test_the_row_records_when_why_and_which_message(self, store):
        commitment = await held(store)
        await end(store, ended=[commitment], reason="event", message=("s2", "e9"), now=NOW)
        row = await store.fetchrow(
            """
            SELECT ended_at, ended_reason, ended_by_session_id, ended_by_entry_id
            FROM memories WHERE id = $1
            """,
            commitment,
        )
        assert tuple(row) == (NOW, "event", "s2", "e9")

    async def test_the_statements_that_ended_come_back(self, store):
        first, second = await held(store, "first"), await held(store, "second")
        assert await end(store, ended=[first, second], reason="reread") == [first, second]

    async def test_a_statement_that_already_ended_does_not_end_again(self, store):
        commitment = await held(store)
        await end(store, ended=[commitment], reason="event", message=("s2", "e9"), now=NOW)
        assert await end(store, ended=[commitment], reason="reread") == []

    async def test_a_replaced_statement_does_not_end(self, store):
        older, newer = await held(store, "older"), await held(store, "newer")
        await retire(store, replaced=[older], replacement=newer)
        assert await end(store, ended=[older], reason="reread") == []

    async def test_an_ended_statement_is_not_replaced(self, store):
        older, newer = await held(store, "older"), await held(store, "newer")
        await end(store, ended=[older], reason="reread")
        assert await retire(store, replaced=[older], replacement=newer) == []

    async def test_the_store_refuses_a_reason_it_does_not_know(self, store):
        commitment = await held(store)
        with pytest.raises(asyncpg.CheckViolationError):
            await end(store, ended=[commitment], reason="finished")


@pytest.fixture
async def one_ended(store: asyncpg.Pool) -> asyncpg.Pool:
    """A commitment that ended, beside one that did not."""
    ended = await held(store)
    await held(store, "The widget's tests run against a real Postgres.", kind="semantic")
    await end(store, ended=[ended], reason="event", message=("s2", "e9"), now=NOW)
    return store


async def test_an_ended_statement_is_absent_from_the_read(one_ended):
    found = await memories(one_ended, scope_key=SCOPE, now=NOW)
    assert [row["statement"] for row in found] == [
        "The widget's tests run against a real Postgres."
    ]


async def test_an_ended_statement_is_not_offered_to_replace(one_ended):
    found = await standing(
        one_ended, scope_key=SCOPE, kinds=["prospective", "semantic"], said_before=NOW
    )
    assert COMMITMENT not in {row["statement"] for row in found}


async def test_an_ended_statement_is_absent_from_the_opening_list(one_ended):
    found = await opening(one_ended, scope_key=SCOPE, seen=(), limit=10, now=NOW)
    assert COMMITMENT not in {row["statement"] for row in found}


async def test_an_ended_statement_is_not_embedded(one_ended, embedder: Embedder):
    assert await embed_missing(one_ended, embedder) == 1


async def test_an_ended_statement_is_counted_as_retired(one_ended):
    found = await metrics(one_ended)
    assert (found["gauges"]["memories_live"], found["gauges"]["memories_retired"]) == (1, 1)


@pytest.mark.parametrize("ends, expected", [(False, True), (True, False)], ids=["live", "ended"])
async def test_an_ended_statement_is_absent_from_the_match(
    store, embedder: Embedder, ends, expected
):
    # Twelve statements about other things, so the scope has a baseline for
    # the commitment to stand above.
    for number in range(12):
        await held(store, f"The garden hose number {number} is green.", kind="semantic")
    commitment = await held(store)
    await embed_missing(store, embedder)
    if ends:
        await end(store, ended=[commitment], reason="event", message=("s2", "e9"))
    found = await match(
        store,
        embedder.query("is the widget release still held for the security review?"),
        model=embedder.model,
        seen=(),
        scope_key=SCOPE,
        limit=20,
        margin=0.0,
        actionable=0.0,
        now=NOW,
    )
    assert (COMMITMENT in {row["statement"] for row in found}) == expected


MODEL = "BAAI/bge-small-en-v1.5"
BASE = literal([1.0] + [0.0] * 383)


async def test_an_ended_statement_is_not_merged(store):
    ended, newer = await held(store), await held(store, COMMITMENT + " ", said_at=NOW)
    await store.execute(
        "UPDATE memories SET embedding = $1::vector, embedding_model = $2", BASE, MODEL
    )
    await end(store, ended=[ended], reason="event", message=("s2", "e9"))
    merged = await merge(
        store, None, statement_id=newer, model=MODEL, settings=Settings(embed_model=MODEL)
    )
    assert merged == []
    assert await store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", ended) is None


@pytest.mark.parametrize(
    "until_moment, expected",
    [
        (None, True),
        (NOW + timedelta(hours=1), True),
        (NOW - timedelta(hours=1), False),
    ],
    ids=["no-moment", "before-its-moment", "after-its-moment"],
)
async def test_a_commitment_is_absent_from_the_read_after_its_moment(store, until_moment, expected):
    await held(store, until_moment=until_moment)
    found = await memories(store, scope_key=SCOPE, now=NOW)
    assert bool(found) == expected


@pytest.mark.parametrize(
    "hours_from_now, expected", [(24, True), (-24, False)], ids=["ahead", "passed"]
)
async def test_a_commitment_is_absent_from_the_opening_list_after_its_moment(
    store, hours_from_now, expected
):
    # A turn reads the table as it is, so the moment is compared with the
    # clock of the store.
    await held(store, until_moment=datetime.now(UTC) + timedelta(hours=hours_from_now))
    found = await opening(store, scope_key=SCOPE, seen=(), limit=10, now=NOW)
    assert bool(found) == expected


async def test_a_commitment_past_its_moment_is_not_offered_to_replace(store):
    await held(store, until_moment=NOW - timedelta(hours=1))
    found = await standing(store, scope_key=SCOPE, kinds=["prospective"], said_before=NOW)
    assert found == []


async def test_every_partial_index_leaves_out_an_ended_statement(store):
    found = await store.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memories'"
        " AND indexdef LIKE '%WHERE%'"
    )
    assert {row["indexname"] for row in found} == {"memories_live_scope", "memories_live_embedding"}
    assert all("ended_at IS NULL" in row["indexdef"] for row in found)


async def test_a_store_with_the_older_indexes_takes_the_new_ones(store):
    await store.execute("DROP INDEX memories_live_scope")
    await store.execute(
        "CREATE INDEX memories_live ON memories (scope_key) WHERE superseded_by IS NULL"
    )
    await store.execute(SCHEMA.read_text())
    names = {row["indexname"] for row in await store.fetch("SELECT indexname FROM pg_indexes")}
    assert "memories_live" not in names
    assert "memories_live_scope" in names
