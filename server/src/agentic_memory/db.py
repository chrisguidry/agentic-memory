"""The store: the pool, the schema, and reading back what is in it.

Writing a batch is in `ingest`, which is the one part of the service that
touches all four tables at once.
"""

import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger("agentic_memory")


async def open_pool(database_url: str, *, size: int = 4) -> asyncpg.Pool:
    """The pool every part of this service uses.

    A jsonb codec was tried here and removed. It decoded a jsonb value returned
    from a function and left a jsonb column as text, on the same connection, so
    a query that read a column got a string and one that built an object got a
    dict. Worse, its encoder ran a second json.dumps on text that was already
    encoded, so an object was stored as a JSON string and no key could be read
    out of it. A reader asks Postgres for the field it wants, which behaves the
    same way everywhere.
    """
    return await asyncpg.create_pool(database_url, min_size=1, max_size=size)


@asynccontextmanager
async def store_pool():
    """The store, opened once and shared by every task on a worker.

    One pool serves both tasks: the reader and the writer are the same kind of
    work, and giving each its own would open two pools for no reason.
    """
    from .settings import get_settings

    pool = await open_pool(get_settings().database_url)
    try:
        yield pool
    finally:
        await pool.close()


SUMMARY = (
    "id, occurred_at, session_id, entry_id, harness, machine, actor, "
    "actor_depth, root, kind, operation, provider, model, tool_name, "
    "scope_key, scope_kind, owner, repository, revision, working_directory, "
    "input_tokens, output_tokens, cost_total, error_type, left(body, 200) AS body"
)


async def count(pool: asyncpg.Pool) -> dict[str, int]:
    """How much the store holds.

    One statement, so the numbers describe the same moment. Counted one at a
    time they disagree while a load is running, which reads as a bug in the
    writing rather than a bug in the counting.
    """
    query = """
        SELECT (SELECT count(*) FROM otel_exports)     AS exports,
               (SELECT count(*) FROM logs)            AS logs,
               (SELECT count(*) FROM resources)       AS resources,
               (SELECT count(*) FROM scopes)          AS scopes,
               (SELECT count(*) FROM classifications) AS classifications,
               (SELECT count(*) FROM otel_exports WHERE unpacked_at IS NULL) AS pending
    """
    async with pool.acquire() as connection:
        found = await connection.fetchrow(query)
    return dict(found)


METRICS = """
    SELECT (SELECT count(*) FROM otel_exports)                                 AS exports,
           (SELECT count(*) FROM otel_exports WHERE unpacked_at IS NULL)       AS pending,
           (SELECT count(*) FROM logs)                                         AS logs,
           (SELECT count(*) FROM resources)                                    AS resources,
           (SELECT count(*) FROM scopes)                                       AS scopes,
           (SELECT count(*) FROM classifications)                              AS classifications,
           (SELECT count(*) FROM memories WHERE superseded_by IS NULL)         AS memories_live,
           (SELECT count(*) FROM memories WHERE superseded_by IS NOT NULL)     AS memories_retired
"""


async def metrics(pool: asyncpg.Pool) -> dict[str, Any]:
    """The numbers a scrape reads.

    Two round trips rather than one: the gauges describe one moment, and the
    model calls need a label per task. A scrape runs every thirty seconds, so
    the counts can afford to walk a table that only grows.
    """
    gauges = dict(await pool.fetchrow(METRICS))
    calls = await pool.fetch(
        "SELECT task, count(*) AS calls FROM model_calls GROUP BY task ORDER BY task"
    )
    return {"gauges": gauges, "calls": [(row["task"], row["calls"]) for row in calls]}


async def recent(
    pool: asyncpg.Pool,
    *,
    scope_key: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The most recent records, narrowed by whichever filters are given."""
    clauses: list[str] = []
    values: list[Any] = []

    for column, wanted in (
        ("scope_key", scope_key),
        ("kind", kind),
        ("session_id", session_id),
        ("harness", harness),
    ):
        if wanted is not None:
            values.append(wanted)
            clauses.append(f"{column} = ${len(values)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)

    query = f"""
        SELECT {SUMMARY}
        FROM logs
        {where}
        ORDER BY occurred_at DESC NULLS LAST, id DESC
        LIMIT ${len(values)}
    """

    async with pool.acquire() as connection:
        found = await connection.fetch(query, *values)
    return [dict(record) for record in found]


async def classified(
    pool: asyncpg.Pool,
    *,
    kinds: Sequence[str],
    scope_key: str | None = None,
    session_id: str | None = None,
    above: float = 0.0,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The messages the classifier read, highest first.

    The threshold arrives with the query rather than sitting in the table,
    because the reading is a probability and the decision about it belongs to
    whoever is asking. Name a kind to measure that kind, because the kinds do
    not fire at the same rate and one number across them filters very little.
    Name none to get the most recent readings, which is what watching the loop
    needs.

    The message comes back with the reading, because calibration is reading a
    page of messages next to what the model made of them.
    """
    # The kind names are the column names, and the module that asks the
    # questions owns them. Taking them here rather than importing them keeps
    # this module from depending on the reader.
    if kind is not None and kind not in kinds:
        raise ValueError(f"unknown kind {kind!r}")

    readings = (
        "SELECT session_id, entry_id, scope_key, model, questions_fingerprint,"
        " rounds, state->>'message' AS message,"
        f" {', '.join(kinds)}, classified_at"
        " FROM classifications"
    )

    if kind is None:
        query = (
            readings
            + " WHERE ($1::text IS NULL OR scope_key = $1)"
            + " AND ($2::text IS NULL OR session_id = $2)"
            + " ORDER BY classified_at DESC LIMIT $3"
        )
        values = (scope_key, session_id, limit)
    else:
        query = (
            readings
            + f" WHERE {kind} >= $1 AND ({kind} IS NOT NULL)"
            + " AND ($2::text IS NULL OR scope_key = $2)"
            + " AND ($3::text IS NULL OR session_id = $3)"
            + f" ORDER BY {kind} DESC LIMIT $4"
        )
        values = (above, scope_key, session_id, limit)

    async with pool.acquire() as connection:
        found = await connection.fetch(query, *values)
    return [dict(record) for record in found]


# The schema is one file, and every statement in it can be applied to a
# database that has the tables or to one that does not. It is applied when the
# service starts, so the file and the database cannot disagree. A column added
# to the file and migrated by hand somewhere else is how they disagree.
SCHEMA = Path(__file__).resolve().parents[2] / "schema.sql"


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Bring the database up to the schema in the file."""
    async with pool.acquire() as connection:
        await connection.execute(SCHEMA.read_text())
