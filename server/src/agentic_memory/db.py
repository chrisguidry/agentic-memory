"""Writing records to Postgres.

Four tables are written together. `resources` and `scopes` hold the two maps
every signal has, `otel_exports` holds what arrived, and `logs` holds the
unpacked form.

The raw table does the deduplication, rather than the unpacked one, because
the unpacked table is a projection of the raw one and cannot hold anything the
raw one does not.
"""

import json
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

import asyncpg

from .otlp import raw, resource_row, row, scope_row

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


class Stored(NamedTuple):
    """What a batch did, and the prompts in it that were written.

    The prompts leave here rather than being found later, because the caller
    schedules them and a second query would have to guess which of them were
    new. The body goes with them so the caller can tell the person's own words
    from what the harness wrote for itself.
    """

    received: int
    inserted: int
    repeated: int
    prompts: list[tuple[str, str, str]]

    @property
    def counted(self) -> dict[str, int]:
        """The part of this an OTLP response reports."""
        return {
            "received": self.received,
            "inserted": self.inserted,
            "repeated": self.repeated,
        }


# One statement for the batch, returning only the rows that were new, so the
# unpack writes only those and a repeat costs a write and nothing else.
RAW_INSERT = """
    INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)
    SELECT * FROM unnest($1::text[], $2::text[], $3::bigint[], $4::bigint[], $5::jsonb[])
    ON CONFLICT (session_id, entry_id) DO NOTHING
    RETURNING id, session_id, entry_id, received_at, resource_id, scope_id
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


async def store(pool: asyncpg.Pool, arrived: list[tuple[dict, dict, dict]]) -> Stored:
    """Write a batch and report what happened to it.

    `arrived` is a list of `(resource, scope, record)`, which is what the OTLP
    walk yields.
    """
    if not arrived:
        return Stored(received=0, inserted=0, repeated=0, prompts=[])

    async with pool.acquire() as connection, connection.transaction():
        raws = [
            {
                **raw(record),
                "resource_id": await _resource_id(connection, resource),
                "scope_id": await _scope_id(connection, scope),
                "payload": resource,
            }
            for resource, scope, record in arrived
        ]

        fresh = await connection.fetch(
            RAW_INSERT,
            [found["session_id"] for found in raws],
            [found["entry_id"] for found in raws],
            [found["resource_id"] for found in raws],
            [found["scope_id"] for found in raws],
            [found["record"] for found in raws],
        )

        prompts = await _unpack(connection, raws, fresh) if fresh else []

    return Stored(
        received=len(arrived),
        inserted=len(fresh),
        repeated=len(arrived) - len(fresh),
        prompts=prompts,
    )


async def _unpack(
    connection: asyncpg.Connection,
    raws: list[dict[str, Any]],
    fresh: list[asyncpg.Record],
) -> list[tuple[str, str]]:
    """Write the unpacked form of the records that were new, and name the prompts."""
    # The raw insert returns the session and the entry, so the record a row
    # belongs to is found by the pair it was built from.
    by_entry = {(found["session_id"], found["entry_id"]): index for index, found in enumerate(raws)}

    picked = [
        (index, found)
        for found in fresh
        if (index := by_entry.get((found["session_id"], found["entry_id"]))) is not None
    ]
    prompts = await _write_logs(
        connection,
        [raws[index]["payload"] for index, _ in picked],
        [raws[index]["record"] for index, _ in picked],
        [found for _, found in picked],
    )
    await connection.execute(MARK_UNPACKED, [found["id"] for found in fresh])
    return prompts


async def _write_logs(
    connection: asyncpg.Connection,
    resources: list[dict],
    records: list[str],
    exports: list[asyncpg.Record],
) -> list[tuple[str, str, str]]:
    """Write the unpacked rows for a batch of raw records.

    The record is unpacked here rather than at the door, so a change to the
    unpack reaches records already stored. The arrival time is copied from the
    export rather than taken from now, for the same reason: a rebuild reads old
    records and must not change when they arrived.

    A prompt is the only record worth reading again, and only one with an entry
    id of its own can be pointed at, so those come back named.
    """
    values = []
    prompts: list[tuple[str, str, str]] = []
    for resource, record, export in zip(resources, records, exports, strict=True):
        unpacked = row(resource, {}, json.loads(record))
        unpacked["export_id"] = export["id"]
        unpacked["received_at"] = export["received_at"]
        unpacked["resource_id"] = export["resource_id"]
        unpacked["scope_id"] = export["scope_id"]
        values.append(tuple(unpacked.get(column) for column in LOG_COLUMNS))

        if (
            unpacked.get("kind") == "prompt"
            and unpacked.get("session_id")
            and unpacked.get("entry_id")
        ):
            prompts.append(
                (unpacked["session_id"], unpacked["entry_id"], unpacked.get("body") or "")
            )

    if values:
        await connection.executemany(LOG_INSERT, values)
    return prompts


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
                # The unpack names the prompts in the batch, and a rebuild has
                # no one to hand them to, so they are dropped here.
                await _write_logs(
                    connection,
                    [json.loads(row["payload"]) for row in found],
                    [row["record"] for row in found],
                    found,
                )
                await connection.execute(MARK_UNPACKED, [row["id"] for row in found])
            written += len(found)

    return {"rebuilt": written}


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
