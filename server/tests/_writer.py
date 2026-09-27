"""What the writer's tests share: a reading, a store, a model, and the record.

The fake store answers the reading and the held statements, and records the
writes. The real-store helpers write the rows a message leaves in the record,
for the tests whose point is a query.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg

from agentic_memory.classify import KIND_COLUMNS, questions_fingerprint

# When the message was said. The ranking ages a statement by this rather than
# by when the statement was written, so the writer records it on the row.
SAID = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def reading(**overrides) -> dict:
    """One classification row, as the store hands it back."""
    scores = dict.fromkeys(KIND_COLUMNS, 0.05)
    found = {
        "session_id": "s1",
        "entry_id": "e1",
        "scope_key": "github.com/liken-sh",
        "model": "jev-1.13.0",
        "questions_fingerprint": questions_fingerprint(),
        "before": "[person] earlier",
        "message": "we use uv, not pip",
        "beyond_this_project": 0.1,
        "forbids": 0.1,
        "said_at": SAID,
        "actor": "person",
        "actor_depth": 0,
    }
    found.update(scores)
    found.update(overrides)
    return found


class FakeStore:
    """A store that answers the reading, the held statements, and the writes."""

    def __init__(self, found, standing: list[dict] | None = None):
        self.found = found
        self.standing = standing or []
        self.written: list[tuple] = []
        self.retired: list[tuple] = []
        self.asked_standing: tuple | None = None

    async def fetchrow(self, query, *values):
        return self.found

    async def fetch(self, query, *values):
        self.asked_standing = values
        return self.standing

    async def fetchval(self, query, *values):
        self.written.append(values)
        return len(self.written)

    async def execute(self, query, *values):
        self.retired.append(values)
        return "UPDATE 1"


class FakeModel:
    """A model that replies with whatever it was handed."""

    def __init__(self, reply: str):
        self.reply = reply
        self.asked: str | None = None

    async def post(self, url, *, headers, json):
        self.asked = json["messages"][1]["content"]
        self.system = json["messages"][0]["content"]
        self.limit = json["max_tokens"]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": self.reply}}]},
        )


def replying(*statements, replaces=None, everywhere=None) -> FakeModel:
    return FakeModel(
        json.dumps(
            [
                {
                    "kind": kind,
                    "statement": statement,
                    "replaces": replaces,
                    **({"everywhere": everywhere} if everywhere is not None else {}),
                }
                for kind, statement in statements
            ]
        )
    )


def held(statement_id: int, kind: str = "preference", statement: str = "Postgres is the store."):
    """One statement the place already holds, as the store hands it back."""
    return {
        "id": statement_id,
        "kind": kind,
        "statement": statement,
        "score": 0.9,
        "scope_key": "github.com/liken-sh",
        "session_id": "s0",
        "entry_id": "e0",
        "model": "jev-1.13.0",
        "said_at": SAID,
        "created_at": None,
    }


async def said(store: asyncpg.Pool, entry_id: str, at: datetime | None) -> None:
    """The record of one message, said at a moment, or at none."""
    # The maps every record points at, made once. A statement cannot read a
    # row its own CTE inserted, so these are two statements of their own.
    await store.execute(
        "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') ON CONFLICT DO NOTHING"
    )
    await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
    export_id = await store.fetchval(
        """
        INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)
        VALUES ('s1', $1, (SELECT id FROM resources), (SELECT id FROM scopes), '{}')
        RETURNING id
        """,
        entry_id,
    )
    await store.execute(
        """
        INSERT INTO logs
            (export_id, received_at, resource_id, scope_id, occurred_at, attributes,
             session_id, entry_id, kind)
        VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',
                's1', $3, 'prompt')
        """,
        export_id,
        at,
        entry_id,
    )


async def read(
    store: asyncpg.Pool,
    entry_id: str,
    classified_at: datetime,
    scope_key: str | None = None,
    **scores,
) -> None:
    """One reading that cleared the correction threshold, read at a moment."""
    columns = dict.fromkeys(KIND_COLUMNS, 0.05) | {"correction": 0.9} | scores
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, scope_key, model, questions_fingerprint, rounds, state,
             classified_at, {", ".join(columns)})
        VALUES ('s1', $1, $2, 'jev-1.13.0', $3, 5, '{{"message": "a message"}}', $4,
                {", ".join(f"${number}" for number in range(5, 5 + len(columns)))})
        """,
        entry_id,
        scope_key,
        questions_fingerprint(),
        classified_at,
        *columns.values(),
    )
