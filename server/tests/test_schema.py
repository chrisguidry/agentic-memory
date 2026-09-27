"""Applying the schema when a process starts.

The service and the worker both apply the schema when they start, because
either one can roll out first and the other then runs against a store the new
code does not fit. Two processes that start together apply it at once, and
Postgres does not make two concurrent `CREATE ... IF NOT EXISTS` safe, so the
two take turns under a lock.
"""

import asyncio
from collections.abc import AsyncIterator

import _postgres
import asyncpg
import pytest

from agentic_memory.db import apply_schema, open_pool
from agentic_memory.worker import prepare_store


@pytest.fixture
async def blank_url(postgres_port: int, request: pytest.FixtureRequest) -> AsyncIterator[str]:
    """A database with nothing in it, not even the template's schema."""
    name = f"blank_{request.node.name[-40:].lower()}".replace("[", "_").replace("]", "_")
    connection = await asyncpg.connect(_postgres.url(postgres_port, "postgres"))
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    yield _postgres.url(postgres_port, name)


async def tables(url: str) -> set[str]:
    connection = await asyncpg.connect(url)
    try:
        found = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    finally:
        await connection.close()
    return {row["tablename"] for row in found}


async def test_two_processes_that_start_together_both_apply_the_schema(blank_url: str):
    first, second = await open_pool(blank_url, size=1), await open_pool(blank_url, size=1)
    try:
        await asyncio.gather(apply_schema(first), apply_schema(second))
    finally:
        await first.close()
        await second.close()
    assert "memories" in await tables(blank_url)


async def test_the_worker_applies_the_schema_when_it_starts(blank_url: str):
    await prepare_store(blank_url)
    assert "memories" in await tables(blank_url)
