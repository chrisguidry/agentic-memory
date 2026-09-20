"""Writing records to Postgres."""

import logging
from typing import Any

import asyncpg

log = logging.getLogger("agentic_memory")

# The columns, in the order the insert names them.
COLUMNS = (
    "resource",
    "instrumentation_scope",
    "record",
    "occurred_at",
    "observed_at",
    "severity",
    "body",
    "session_id",
    "previous_session",
    "entry_id",
    "harness",
    "machine",
    "actor",
    "actor_depth",
    "root",
    "kind",
    "operation",
    "provider",
    "model",
    "response_model",
    "tool_name",
    "scope",
    "scope_kind",
    "repository",
    "owner",
    "revision",
    "working_directory",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_total",
    "error_type",
    "trace_id",
    "span_id",
    "attributes",
)

# These four arrive as JSON text and are stored as jsonb.
JSONB = ("resource", "instrumentation_scope", "record", "attributes")

PLACEHOLDERS = ", ".join(
    f"${index}::jsonb" if column in JSONB else f"${index}"
    for index, column in enumerate(COLUMNS, start=1)
)

# `ON CONFLICT DO NOTHING` with no target covers the partial unique index on
# the entry id, so a record that arrives twice is stored once.
INSERT = f"""
    INSERT INTO otlp_log_records ({", ".join(COLUMNS)})
    VALUES ({PLACEHOLDERS})
    ON CONFLICT DO NOTHING
"""

# What the inspection endpoint returns, so a person reads a summary rather
# than a whole envelope.
SUMMARY = (
    "id, received_at, occurred_at, session_id, entry_id, harness, machine, "
    "actor, actor_depth, root, kind, operation, provider, model, tool_name, "
    "scope, scope_kind, owner, repository, revision, working_directory, "
    "input_tokens, output_tokens, cost_total, error_type, "
    "left(body, 200) AS body"
)


async def store(pool: asyncpg.Pool, rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Write rows and report how many were new, repeats, and unstorable.

    A record that is already stored lands on the unique index and counts as
    a repeat rather than an error, so re-sending a record costs a write and
    nothing else. The status line from Postgres is the only place the real
    count appears, because `ON CONFLICT DO NOTHING` reports nothing else.

    A record the store cannot hold is retried on its own rather than costing
    the rest of the batch. One transcript out of a backfill should not be
    able to lose every record beside it.
    """
    if not rows:
        return 0, 0, 0

    async with pool.acquire() as connection:
        try:
            return await _write(connection, rows)
        except asyncpg.PostgresError as problem:
            log.warning("a batch failed (%s); retrying one row at a time", problem)
            return await _write_apart(connection, rows)


async def _write(
    connection: asyncpg.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Write the batch inside one transaction."""
    inserted = 0
    async with connection.transaction():
        for row in rows:
            status = await connection.execute(INSERT, *(row[column] for column in COLUMNS))
            inserted += status.endswith(" 1")
    return inserted, len(rows) - inserted, 0


async def _write_apart(
    connection: asyncpg.Connection, rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Write each row on its own, so one failure does not take the others."""
    inserted = repeated = failed = 0
    for row in rows:
        try:
            async with connection.transaction():
                status = await connection.execute(INSERT, *(row[column] for column in COLUMNS))
            if status.endswith(" 1"):
                inserted += 1
            else:
                repeated += 1
        except asyncpg.PostgresError as problem:
            failed += 1
            log.warning("a record could not be stored: %s", problem)
    return inserted, repeated, failed


async def count(pool: asyncpg.Pool) -> int:
    """How many records the store holds."""
    async with pool.acquire() as connection:
        return await connection.fetchval("SELECT count(*) FROM otlp_log_records")


async def recent(
    pool: asyncpg.Pool,
    *,
    scope: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The most recent records, narrowed by whichever filters are given."""
    clauses: list[str] = []
    values: list[Any] = []

    for column, wanted in (
        ("scope", scope),
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
        FROM otlp_log_records
        {where}
        ORDER BY occurred_at DESC NULLS LAST, id DESC
        LIMIT ${len(values)}
    """

    async with pool.acquire() as connection:
        found = await connection.fetch(query, *values)
    return [dict(row) for row in found]
