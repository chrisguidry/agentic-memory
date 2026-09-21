"""Reading back what the classifier wrote.

These cover the query the service builds. The reading is a row of scores, and
the two ways to ask for it measure different things: a named kind measures that
kind, and no name gets the most recent readings for watching the loop.
"""

import pytest

from agentic_memory.db import classified

KINDS = ("semantic", "procedural", "correction", "praise")


class RecordingStore:
    """A store that keeps the query it was asked for instead of running it."""

    def __init__(self):
        self.query: str | None = None
        self.values: tuple | None = None

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *values):
        self.query, self.values = query, values
        return []


class TestClassified:
    async def test_naming_no_kind_gets_the_most_recent_readings(self):
        store = RecordingStore()
        await classified(store, kinds=KINDS, limit=30)
        assert "ORDER BY classified_at DESC" in store.query
        assert store.values == (None, None, 30)

    async def test_naming_no_kind_does_not_compare_a_score_to_a_threshold(self):
        # The first version of this compared classified_at to the threshold, and
        # Postgres refused it because the other cast had made the parameter text.
        store = RecordingStore()
        await classified(store, kinds=KINDS)
        for kind in KINDS:
            assert f"{kind} >=" not in store.query

    async def test_naming_a_kind_measures_that_kind(self):
        store = RecordingStore()
        await classified(store, kinds=KINDS, kind="correction", above=0.7)
        assert "correction >= $1" in store.query
        assert "ORDER BY correction DESC" in store.query
        assert store.values[0] == 0.7

    async def test_a_kind_the_reader_does_not_ask_about_is_refused(self):
        with pytest.raises(ValueError):
            await classified(RecordingStore(), kinds=KINDS, kind="forbids")

    async def test_the_message_comes_back_with_the_reading(self):
        store = RecordingStore()
        await classified(store, kinds=KINDS)
        assert "state->>'message' AS message" in store.query
