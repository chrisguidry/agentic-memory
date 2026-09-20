"""Writing records to Postgres.

Three tables are written together. `resources` and `scopes` hold the two maps
every signal carries, `otel_exports` holds what arrived, and `logs` holds the
unpacked form.

Deduplication lives in the raw table rather than in the unpacked one, because
the unpacked table is a projection of the raw one and cannot hold anything the
raw one does not.
"""

import json
import logging
from typing import Any

import asyncpg

from .otlp import raw, resource_row, row, scope_row

log = logging.getLogger("agentic_memory")

# One statement for the batch, returning only the rows that were new, so the
# unpack knows exactly what to write and a repeat costs a write and nothing
# else.
RAW_INSERT = """
    INSERT INTO otel_exports (content_hash, resource_id, scope_id, record)
    SELECT * FROM unnest($1::text[], $2::bigint[], $3::bigint[], $4::jsonb[])
    ON CONFLICT (content_hash) DO NOTHING
    RETURNING id, content_hash, received_at, resource_id, scope_id
"""

RESOURCE_UPSERT = """
    INSERT INTO resources (fingerprint, resource)
    VALUES ($1, $2::jsonb)
    ON CONFLICT (fingerprint) DO NOTHING
"""

RESOURCE_FIND = "SELECT id FROM resources WHERE fingerprint = $1"

SCOPE_UPSERT = """
    INSERT INTO scopes (fingerprint, name, version, attributes)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (fingerprint) DO NOTHING
"""

SCOPE_FIND = "SELECT id FROM scopes WHERE fingerprint = $1"

# A resource map and a scope map are immutable and there are two of each, so
# remembering the ids costs nothing and keeps the lookup off the database for
# everything but the first sighting of each.
_resource_ids: dict[str, int] = {}
_scope_ids: dict[str, int] = {}

LOG_COLUMNS = (
    "export_id",
    "received_at",
    "resource_id",
    "scope_id",
    "occurred_at",
    "observed_at",
    "severity",
    "severity_number",
    "trace_id",
    "span_id",
    "body",
    "attributes",
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

JSONB = ("attributes",)

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


async def _resource_id(connection: asyncpg.Connection, resource: dict) -> int:
    """The id of a resource map, written if this is the first sighting."""
    found = resource_row(resource)
    known = _resource_ids.get(found["fingerprint"])
    if known is not None:
        return known

    await connection.execute(RESOURCE_UPSERT, found["fingerprint"], found["resource"])
    identifier = await connection.fetchval(RESOURCE_FIND, found["fingerprint"])
    _resource_ids[found["fingerprint"]] = identifier
    return identifier


async def _scope_id(connection: asyncpg.Connection, scope: dict) -> int:
    """The id of a scope map, written if this is the first sighting."""
    found = scope_row(scope)
    known = _scope_ids.get(found["fingerprint"])
    if known is not None:
        return known

    await connection.execute(
        SCOPE_UPSERT,
        found["fingerprint"],
        found["name"],
        found["version"],
        found["attributes"],
    )
    identifier = await connection.fetchval(SCOPE_FIND, found["fingerprint"])
    _scope_ids[found["fingerprint"]] = identifier
    return identifier


async def store(pool: asyncpg.Pool, arrived: list[tuple[dict, dict, dict]]) -> dict[str, int]:
    """Write a batch and report what happened to it.

    `arrived` is a list of `(resource, scope, record)`, which is what the OTLP
    walk yields.
    """
    if not arrived:
        return {"received": 0, "inserted": 0, "repeated": 0}

    async with pool.acquire() as connection, connection.transaction():
        raws = [
            {
                **raw(resource, scope, record),
                "resource_id": await _resource_id(connection, resource),
                "scope_id": await _scope_id(connection, scope),
                "payload": resource,
            }
            for resource, scope, record in arrived
        ]

        fresh = await connection.fetch(
            RAW_INSERT,
            [found["content_hash"] for found in raws],
            [found["resource_id"] for found in raws],
            [found["scope_id"] for found in raws],
            [found["record"] for found in raws],
        )

        if fresh:
            await _unpack(connection, raws, fresh)

    return {
        "received": len(arrived),
        "inserted": len(fresh),
        "repeated": len(arrived) - len(fresh),
    }


async def _unpack(
    connection: asyncpg.Connection,
    raws: list[dict[str, Any]],
    fresh: list[asyncpg.Record],
) -> None:
    """Write the unpacked form of the records that were new."""
    # The hash is what the raw insert returned, so the record it belongs to is
    # found by the hash it was built from.
    by_hash = {found["content_hash"]: index for index, found in enumerate(raws)}

    picked = [
        (index, found)
        for found in fresh
        if (index := by_hash.get(found["content_hash"])) is not None
    ]
    await _write_logs(
        connection,
        [raws[index]["payload"] for index, _ in picked],
        [raws[index]["record"] for index, _ in picked],
        [found for _, found in picked],
    )
    await connection.execute(MARK_UNPACKED, [found["id"] for found in fresh])


async def _write_logs(
    connection: asyncpg.Connection,
    resources: list[dict],
    records: list[str],
    exports: list[asyncpg.Record],
) -> int:
    """Write the unpacked rows for a batch of raw records.

    The record is unpacked here rather than at the door, so a change to the
    unpack reaches records that arrived long ago. The arrival time is copied
    from the export rather than taken from now, for the same reason: a rebuild
    reads old records and must not rewrite when they arrived.
    """
    values = []
    for resource, record, export in zip(resources, records, exports, strict=True):
        unpacked = row(resource, {}, json.loads(record))
        unpacked["export_id"] = export["id"]
        unpacked["received_at"] = export["received_at"]
        unpacked["resource_id"] = export["resource_id"]
        unpacked["scope_id"] = export["scope_id"]
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
                "SELECT e.id, e.received_at, e.resource_id, e.scope_id, e.record, "
                "r.resource AS payload "
                "FROM otel_exports e JOIN resources r ON r.id = e.resource_id "
                "WHERE e.id > $1 ORDER BY e.id LIMIT $2",
                last,
                batch,
            )
            if not found:
                break

            last = found[-1]["id"]
            async with connection.transaction():
                written += await _write_logs(
                    connection,
                    [json.loads(row["payload"]) for row in found],
                    [row["record"] for row in found],
                    found,
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
            "resources": await connection.fetchval("SELECT count(*) FROM resources"),
            "scopes": await connection.fetchval("SELECT count(*) FROM scopes"),
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
