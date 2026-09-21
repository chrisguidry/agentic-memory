"""What every test can ask for.

The store is a real Postgres, started once for each pytest worker and thrown
away at the end. Each test gets a database of its own, copied from a template
the schema was applied to once, so no test sees another's rows and the schema
is never applied more than once per container.
"""

import os
from collections.abc import AsyncIterator, Iterator

import _postgres
import asyncpg
import pytest

from agentic_memory.db import SCHEMA, open_pool

TEMPLATE = "agentic_memory_template"


@pytest.fixture(scope="session")
def postgres_port(worker_id: str) -> Iterator[int]:
    """One server per worker, on a port of its own.

    A worker owns its server outright, so a test never has to share a database
    with a test on another worker and the two never wait on each other.
    """
    try:
        client = _postgres.docker.from_env()
        client.ping()
    except Exception as failure:
        pytest.skip(f"docker is not available: {failure}")

    _postgres.remove_stale(client)
    container, port = _postgres.start(client, f"agentic-memory-{worker_id}-{os.getpid()}")
    try:
        yield port
    finally:
        container.stop()


@pytest.fixture(scope="session")
async def template(postgres_port: int) -> str:
    """The database the schema has been applied to, which every test copies.

    A template rather than a truncate between tests, because a truncate has to
    name every table and forgets the next one the schema adds. A copy of the
    template holds whatever the schema made.
    """
    connection = await asyncpg.connect(_postgres.url(postgres_port, "postgres"))
    try:
        await connection.execute(f"CREATE DATABASE {TEMPLATE}")
    finally:
        await connection.close()

    connection = await asyncpg.connect(_postgres.url(postgres_port, TEMPLATE))
    try:
        await connection.execute(SCHEMA.read_text())
    finally:
        await connection.close()
    return TEMPLATE


@pytest.fixture
async def postgres_url(postgres_port: int, template: str, request: pytest.FixtureRequest) -> str:
    """A fresh database for this test, and the URL that reaches it."""
    # The node id carries the file and the test name, which is what a person
    # wants to see in `\l` when a run is stuck, but it holds characters a
    # database name cannot.
    name = (
        "test_"
        + "".join(character if character.isalnum() else "_" for character in request.node.nodeid)[
            -50:
        ].lower()
    )
    connection = await asyncpg.connect(_postgres.url(postgres_port, "postgres"))
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await connection.execute(f'CREATE DATABASE "{name}" TEMPLATE {template}')
    finally:
        await connection.close()
    return _postgres.url(postgres_port, name)


@pytest.fixture
async def store(postgres_url: str) -> AsyncIterator[asyncpg.Pool]:
    """The store, opened the way the service opens it."""
    pool = await open_pool(postgres_url, size=2)
    try:
        yield pool
    finally:
        await pool.close()
