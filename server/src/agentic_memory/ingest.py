"""Writing a batch of records to the store.

Four tables are written together. `resources` and `scopes` hold the two maps
every signal has, `otel_exports` holds what arrived, and `logs` holds the
unpacked form.

The raw table does the deduplication, rather than the unpacked one, because
the unpacked table is a projection of the raw one and cannot hold anything the
raw one does not.
"""

import json
from collections.abc import Sequence
from typing import Any, NamedTuple

import asyncpg

from .otlp import fingerprint, raw, resource_row, row, scope_row


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


async def _map_ids(
    connection: asyncpg.Connection,
    table: str,
    upsert: str,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    """The id of every map in a batch, by fingerprint, written on first sighting.

    The batch is looked up in one query rather than each record on its own, and
    nothing is remembered between batches. A cache across batches was keyed by
    the fingerprint alone, and it handed a connection to one database the ids
    from another, which is what a test suite with a database per test does and
    what two stores on one worker would do.
    """
    by_fingerprint = {found["fingerprint"]: found for found in rows}
    for found in by_fingerprint.values():
        await connection.execute(upsert, *found.values())
    known = await connection.fetch(
        f"SELECT id, fingerprint FROM {table} WHERE fingerprint = ANY($1::text[])",
        list(by_fingerprint),
    )
    return {found["fingerprint"]: found["id"] for found in known}


async def store(pool: asyncpg.Pool, arrived: list[tuple[dict, dict, dict]]) -> Stored:
    """Write a batch and report what happened to it.

    `arrived` is a list of `(resource, scope, record)`, which is what the OTLP
    walk yields.
    """
    if not arrived:
        return Stored(received=0, inserted=0, repeated=0, prompts=[])

    async with pool.acquire() as connection, connection.transaction():
        resource_ids = await _map_ids(
            connection,
            "resources",
            RESOURCE_UPSERT,
            [resource_row(resource) for resource, _, _ in arrived],
        )
        scope_ids = await _map_ids(
            connection, "scopes", SCOPE_UPSERT, [scope_row(scope) for _, scope, _ in arrived]
        )
        raws = [
            {
                **raw(record),
                "resource_id": resource_ids[fingerprint(resource)],
                "scope_id": scope_ids[fingerprint(scope)],
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


def matched(raws: list[dict[str, Any]], fresh: Sequence[Any]) -> list[tuple[int, Any]]:
    """Each new row beside the index of the record it was built from.

    The raw insert returns the session and the entry, so a row with an entry is
    found by the pair it was built from. A row with no entry has no pair: a
    null never conflicts, so every such record is new, and they come back in
    the order they went in. They are matched in that order, and a batch that
    comes back a different length is a defect rather than something to guess
    about.
    """
    by_entry = {
        (found["session_id"], found["entry_id"]): index
        for index, found in enumerate(raws)
        if found["entry_id"] is not None
    }
    unnamed = iter(index for index, found in enumerate(raws) if found["entry_id"] is None)

    picked = []
    for found in fresh:
        if found["entry_id"] is None:
            picked.append((next(unnamed), found))
        elif (index := by_entry.get((found["session_id"], found["entry_id"]))) is not None:
            picked.append((index, found))
    return picked


async def _unpack(
    connection: asyncpg.Connection,
    raws: list[dict[str, Any]],
    fresh: list[asyncpg.Record],
) -> list[tuple[str, str, str]]:
    """Write the unpacked form of the records that were new, and name the prompts."""
    picked = matched(raws, fresh)
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
