"""Reading back what the classifier wrote.

These cover the query the service builds. The reading is a row of scores, and
the two ways to ask for it measure different things: a named kind measures that
kind, and no name gets the most recent readings for watching the loop.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.classify import KIND_COLUMNS
from agentic_memory.db import classified, metrics

KINDS = ("semantic", "procedural", "correction", "praise")
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


async def reading(
    store: asyncpg.Pool,
    entry_id: str,
    *,
    scope_key: str | None = "github.com/liken-sh",
    session_id: str = "s1",
    classified_at: datetime = NOW,
    **scores: float,
) -> None:
    """One reading in the store, scored 0.05 on every kind not named."""
    columns = dict.fromkeys(KIND_COLUMNS, 0.05) | scores
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, scope_key, model, questions_fingerprint, rounds,
             state, classified_at, {", ".join(columns)})
        VALUES ($1, $2, $3, 'jev-1.13.0', 'fp', 5, $4::jsonb, $5,
                {", ".join(f"${number}" for number in range(6, 6 + len(columns)))})
        """,
        session_id,
        entry_id,
        scope_key,
        '{"message": "a message", "before": ""}',
        classified_at,
        *columns.values(),
    )


@pytest.fixture
async def three_readings(store: asyncpg.Pool) -> asyncpg.Pool:
    await reading(store, "old", classified_at=NOW - timedelta(hours=2), correction=0.9)
    await reading(store, "mid", classified_at=NOW - timedelta(hours=1), correction=0.5)
    await reading(store, "new", classified_at=NOW, correction=0.2, scope_key="elsewhere")
    return store


class TestClassified:
    async def test_naming_no_kind_gets_the_most_recent_readings(self, three_readings):
        found = await classified(three_readings, kinds=KINDS, limit=2)
        assert [row["entry_id"] for row in found] == ["new", "mid"]

    async def test_naming_no_kind_does_not_compare_a_score_to_a_threshold(self, three_readings):
        found = await classified(three_readings, kinds=KINDS, above=0.99)
        assert len(found) == 3

    async def test_naming_a_kind_measures_that_kind(self, three_readings):
        found = await classified(three_readings, kinds=KINDS, kind="correction", above=0.4)
        assert [row["entry_id"] for row in found] == ["old", "mid"]

    async def test_the_read_is_narrowed_to_the_scope(self, three_readings):
        found = await classified(three_readings, kinds=KINDS, scope_key="elsewhere")
        assert [row["entry_id"] for row in found] == ["new"]

    async def test_the_message_comes_back_with_the_reading(self, three_readings):
        found = await classified(three_readings, kinds=KINDS, limit=1)
        assert found[0]["message"] == "a message"

    async def test_a_kind_the_reader_does_not_ask_about_is_refused(self, store):
        with pytest.raises(ValueError):
            await classified(store, kinds=KINDS, kind="forbids")


class TestMetrics:
    """The numbers a scrape reads, which a dashboard and an alert both use."""

    async def test_a_live_memory_is_counted_and_a_retired_one_is_not(self, store):
        retired = await store.fetchval(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model,
                 questions_fingerprint)
            VALUES ('Postgres is the store.', 'preference', 0.9, 's1', 'e1', 'jev', 'fp')
            RETURNING id
            """
        )
        await store.execute(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model,
                 questions_fingerprint, superseded_by)
            VALUES ('Postgres is the database.', 'preference', 0.9, 's1', 'e2', 'jev', 'fp', $1)
            """,
            retired,
        )
        found = await metrics(store)
        assert found["gauges"]["memories_live"] == 1
        assert found["gauges"]["memories_retired"] == 1

    async def test_the_calls_come_back_under_their_task(self, store):
        await store.execute(
            """
            INSERT INTO model_calls (provider, task, input_tokens, outcome)
            VALUES ('typesafe', 'classify', 10, 'ok'), ('deepinfra', 'synthesize', 20, 'ok'),
                   ('deepinfra', 'synthesize', 30, 'ok')
            """
        )
        found = await metrics(store)
        assert dict(found["calls"]) == {"classify": 1, "synthesize": 2}
