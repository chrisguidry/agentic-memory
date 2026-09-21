"""Writing a batch to the store.

These run against a real database, because the batch is written by SQL and a
fake store cannot find a mistake in it. Every record here is invented.
"""

import asyncpg
import pytest

from agentic_memory.ingest import store
from agentic_memory.otlp import walk

MACHINE = {"key": "host.name", "value": {"stringValue": "a-test-machine"}}
PRODUCER = {"name": "agentic-memory.test", "version": "0"}


def text(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def record(body: str, *, session: str = "s1", entry: str | None = "e1") -> dict:
    """One log record, with an entry id or without one."""
    found = [text("session.id", session), text("agentic_memory.kind", "prompt")]
    if entry is not None:
        found.append(text("agentic_memory.entry.id", entry))
    return {"body": {"stringValue": body}, "attributes": found}


def arrived(*records: dict) -> list[tuple[dict, dict, dict]]:
    """What the OTLP walk yields for a batch of these records."""
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": [MACHINE]},
                "scopeLogs": [{"scope": PRODUCER, "logRecords": list(records)}],
            }
        ]
    }
    return list(walk(payload))


@pytest.fixture
def two_without_entries() -> list[tuple[dict, dict, dict]]:
    return arrived(record("first", entry=None), record("second", entry=None))


class TestStore:
    async def test_a_record_is_written_once(self, store: asyncpg.Pool):
        written = await store_batch(store, arrived(record("hello")))
        assert (written.inserted, written.repeated) == (1, 0)
        assert await store.fetchval("SELECT count(*) FROM logs") == 1

    async def test_a_record_sent_twice_is_stored_once(self, store: asyncpg.Pool):
        await store_batch(store, arrived(record("hello")))
        written = await store_batch(store, arrived(record("hello")))
        assert (written.inserted, written.repeated) == (0, 1)
        assert await store.fetchval("SELECT count(*) FROM logs") == 1

    async def test_two_records_with_no_entry_are_both_unpacked(
        self, store: asyncpg.Pool, two_without_entries
    ):
        await store_batch(store, two_without_entries)
        bodies = await store.fetch("SELECT body FROM logs ORDER BY id")
        assert [found["body"] for found in bodies] == ["first", "second"]

    async def test_each_export_is_unpacked_to_its_own_row(
        self, store: asyncpg.Pool, two_without_entries
    ):
        await store_batch(store, two_without_entries)
        exports = await store.fetch("SELECT DISTINCT export_id FROM logs")
        assert len(exports) == 2

    async def test_the_prompts_come_back_named_with_their_bodies(self, store: asyncpg.Pool):
        written = await store_batch(store, arrived(record("hello", entry="e9")))
        assert written.prompts == [("s1", "e9", "hello")]

    async def test_a_prompt_with_no_entry_is_not_named(self, store: asyncpg.Pool):
        written = await store_batch(store, arrived(record("hello", entry=None)))
        assert written.prompts == []


async def store_batch(pool: asyncpg.Pool, batch: list[tuple[dict, dict, dict]]):
    return await store(pool, batch)
