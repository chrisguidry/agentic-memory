"""Writing records to Postgres.

Two tables are written together. `otel_exports` takes what arrived, and a
record that arrives twice lands on its unique index and is not written again.
`logs` takes the unpacked form of whatever was new.

Deduplication lives in the raw table rather than in the unpacked one, because
the unpacked table is a projection of the raw one and cannot hold anything the
raw one does not.
"""

import json
import logging
from typing import Any

import asyncpg

from .otlp import raw, row

log = logging.getLogger("agentic_memory")

# One statement for the batch, returning only the rows that were new, so the
# unpack knows exactly what to write and a repeat costs a write and nothing
# else.
RAW_INSERT = """
    INSERT INTO otel_exports (content_hash, resource, scope, record)
    SELECT * FROM unnest($1::text[], $2::jsonb[], $3::jsonb[], $4::jsonb[])
    ON CONFLICT (content_hash) DO NOTHING
    RETURNING id, content_hash, received_at
"""

LOG_COLUMNS = (
    "export_id",
    "received_at",
    "occurred_at",
    "observed_at",
    "severity",
    "severity_number",
    "trace_id",
    "span_id",
    "body",
    "attributes",
    "resource",
    "scope_name",
    "scope_version",
    "scope_attributes",
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
    "scope_key",
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
)

JSONB = ("attributes", "resource", "scope_attributes")

PLACEHOLDERS = ", ".join(
    f"${index}::jsonb" if column in JSONB else f"${index}"
    for index, column in enumerate(LOG_COLUMNS, start=1)
)

LOG_INSERT = f"""
    INSERT INTO logs ({", ".join(LOG_COLUMNS)})
    VALUES ({PLACEHOLDERS})
"""

MARK_UNPACKED = "UPDATE otel_exports SET unpacked_at = now() WHERE id = ANY($1::bigint[])"

SUMMARY = (
    "id, occurred_at, session_id, entry_id, harness, machine, actor, "
    "actor_depth, root, kind, operation, provider, model, tool_name, "
    "scope_key, scope_kind, owner, repository, revision, working_directory, "
    "input_tokens, output_tokens, cost_total, error_type, left(body, 200) AS body"
)


async def store(pool: asyncpg.Pool, arrived: list[tuple[dict, dict, dict]]) -> dict[str, int]:
    """Write a batch and report what happened to it.

    `arrived` is a list of `(resource, scope, record)`, which is what the OTLP
    walk yields.
    """
    if not arrived:
        return {"received": 0, "inserted": 0, "repeated": 0, "failed": 0}

    raws = [raw(resource, scope, record) for resource, scope, record in arrived]

    async with pool.acquire() as connection, connection.transaction():
        fresh = await connection.fetch(
            RAW_INSERT,
            [found["content_hash"] for found in raws],
            [found["resource"] for found in raws],
            [found["scope"] for found in raws],
            [found["record"] for found in raws],
        )

        if fresh:
            await _unpack(connection, arrived, raws, fresh)

    return {
        "received": len(arrived),
        "inserted": len(fresh),
        "repeated": len(arrived) - len(fresh),
        "failed": 0,
    }


async def _unpack(
    connection: asyncpg.Connection,
    arrived: list[tuple[dict, dict, dict]],
    raws: list[dict[str, Any]],
    fresh: list[asyncpg.Record],
) -> None:
    """Write the unpacked form of the records that were new."""
    # The hash is what the raw insert returned, so the record it belongs to is
    # found by the hash it was built from.
    by_hash = {found["content_hash"]: index for index, found in enumerate(raws)}

    picked = [
        (index, found["id"], found["received_at"])
        for found in fresh
        if (index := by_hash.get(found["content_hash"])) is not None
    ]
    await _write_logs(
        connection,
        [arrived[index] for index, _, _ in picked],
        [(export_id, received) for _, export_id, received in picked],
    )
    await connection.execute(MARK_UNPACKED, [found["id"] for found in fresh])


async def _write_logs(
    connection: asyncpg.Connection,
    arrived: list[tuple[dict, dict, dict]],
    exports: list[tuple[int, Any]],
) -> int:
    """Write the unpacked rows for a batch of arrived records.

    The arrival time is copied from the export rather than taken from now,
    because a rebuild reads records that arrived long ago and a rebuild must
    not rewrite when they did.
    """
    values = []
    for (resource, scope, record), (export_id, received) in zip(arrived, exports, strict=True):
        unpacked = row(resource, scope, record)
        unpacked["export_id"] = export_id
        unpacked["received_at"] = received
        values.append(tuple(unpacked.get(column) for column in LOG_COLUMNS))

    if values:
        await connection.executemany(LOG_INSERT, values)
    return len(values)


async def rebuild(pool: asyncpg.Pool, batch: int = 500) -> dict[str, int]:
    """Throw the unpacked table away and build it again from the raw one.

    This is what the raw table is for. The extraction can change and the
    record does not have to be sent again.
    """
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("TRUNCATE logs")
        await connection.execute("UPDATE otel_exports SET unpacked_at = NULL")

    written = 0
    last = 0
    while True:
        async with pool.acquire() as connection:
            found = await connection.fetch(
                "SELECT id, received_at, resource, scope, record FROM otel_exports "
                "WHERE id > $1 ORDER BY id LIMIT $2",
                last,
                batch,
            )
            if not found:
                break

            last = found[-1]["id"]
            arrived = [
                (json.loads(row["resource"]), json.loads(row["scope"]), json.loads(row["record"]))
                for row in found
            ]
            async with connection.transaction():
                written += await _write_logs(
                    connection,
                    arrived,
                    [(row["id"], row["received_at"]) for row in found],
                )
                await connection.execute(MARK_UNPACKED, [row["id"] for row in found])

    return {"rebuilt": written}


async def count(pool: asyncpg.Pool) -> dict[str, int]:
    """How much the store holds."""
    async with pool.acquire() as connection:
        return {
            "exports": await connection.fetchval("SELECT count(*) FROM otel_exports"),
            "logs": await connection.fetchval("SELECT count(*) FROM logs"),
            "pending": await connection.fetchval(
                "SELECT count(*) FROM otel_exports WHERE unpacked_at IS NULL"
            ),
        }


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
