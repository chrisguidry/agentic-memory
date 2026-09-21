"""A row for every model call the worker makes.

The worker calls a model in three places. The classifier asks the System One
model which kinds of memory are in a message, the merger asks it whether two
statements say the same thing, and the writer asks a DeepInfra model for the
sentence. Each answer carries a model name, a token count, and a request id,
and the code reads the answer and drops the metadata.

The ledger writes a row where the call happens: a wrapper around each client
reads the context the task set and records the call. A task says what a call is
for by setting that context, so a new call site is recorded without doing
anything of its own.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

import asyncpg
import httpx
from typesafe_sdk import AsyncTypeSafeClient, TypeSafeBadRequestError

log = logging.getLogger("agentic_memory.ledger")

# The two providers the worker calls. The local embedding model calls no API and
# reports no tokens, so it is not in the ledger.
TYPESAFE = "typesafe"
DEEPINFRA = "deepinfra"


@dataclass(frozen=True)
class Context:
    """What a call was for, set by the task around the calls it makes."""

    task: str
    session_id: str | None = None
    entry_id: str | None = None
    run: str = "live"


CURRENT: ContextVar[Context | None] = ContextVar("model_call_context", default=None)


@contextmanager
def calling(
    task: str,
    *,
    session_id: str | None = None,
    entry_id: str | None = None,
    run: str = "live",
) -> Iterator[None]:
    """Name what calls made inside this block are for.

    The context is a variable rather than an argument to each call, so the
    wrapper around a client records a new call site without the call site
    saying anything about the ledger.
    """
    token = CURRENT.set(Context(task, session_id, entry_id, run))
    try:
        yield
    finally:
        CURRENT.reset(token)


# One row per call, in the same token names the record lifts out of the
# harness's telemetry, so one name means one thing across the two sources.
RECORD = """
    INSERT INTO model_calls
        (provider, model, request_id, task, session_id, entry_id, run,
         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         reasoning_tokens, duration_ms, outcome, error_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""


def milliseconds(started: float) -> int:
    """How long a call took, in whole milliseconds."""
    return round((perf_counter() - started) * 1000)


def request_id_of(response: Any) -> str | None:
    """The provider's request id, or nothing when the response carries none."""
    try:
        return response.request_id
    except Exception:
        return None


async def record(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str | None,
    duration_ms: int,
    outcome: str,
    request_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    error_type: str | None = None,
) -> None:
    """Write down one call.

    A ledger write that fails is logged and does not raise, because the call
    already happened and the task still has its answer to read.
    """
    context = CURRENT.get() or Context(task="unknown")
    try:
        await pool.execute(
            RECORD,
            provider,
            model,
            request_id,
            context.task,
            context.session_id,
            context.entry_id,
            context.run,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            duration_ms,
            outcome,
            error_type,
        )
    except Exception:
        log.warning("could not write a model call to the ledger", exc_info=True)


class RecordedSystemOne:
    """An `AsyncTypeSafeClient` that writes a ledger row for each call."""

    def __init__(self, client: AsyncTypeSafeClient, pool: asyncpg.Pool) -> None:
        self.client = client
        self.pool = pool

    async def system_one(self, *, state: Any, questions: Any, **options: Any) -> Any:
        started = perf_counter()
        try:
            response = await self.client.system_one(state=state, questions=questions, **options)
        except TypeSafeBadRequestError as error:
            await record(
                self.pool,
                provider=TYPESAFE,
                model=None,
                request_id=getattr(error, "request_id", None),
                duration_ms=milliseconds(started),
                outcome="refused",
                error_type=type(error).__name__,
            )
            raise
        except Exception as error:
            await record(
                self.pool,
                provider=TYPESAFE,
                model=None,
                duration_ms=milliseconds(started),
                outcome="error",
                error_type=type(error).__name__,
            )
            raise
        usage = getattr(response, "usage", None)
        await record(
            self.pool,
            provider=TYPESAFE,
            model=getattr(response, "model", None),
            request_id=request_id_of(response),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            duration_ms=milliseconds(started),
            outcome="ok",
        )
        return response


class RecordedCompletions:
    """An `httpx.AsyncClient` that writes a ledger row for each completion."""

    def __init__(self, client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
        self.client = client
        self.pool = pool

    async def post(self, url: str, *, headers: dict, json: dict, **options: Any) -> httpx.Response:
        started = perf_counter()
        try:
            response = await self.client.post(url, headers=headers, json=json, **options)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            await record(
                self.pool,
                provider=DEEPINFRA,
                model=None,
                duration_ms=milliseconds(started),
                outcome="refused" if error.response.status_code == 400 else "error",
                error_type=f"http_{error.response.status_code}",
            )
            raise
        except Exception as error:
            await record(
                self.pool,
                provider=DEEPINFRA,
                model=None,
                duration_ms=milliseconds(started),
                outcome="error",
                error_type=type(error).__name__,
            )
            raise
        body = response.json()
        usage = body.get("usage") or {}
        await record(
            self.pool,
            provider=DEEPINFRA,
            model=body.get("model"),
            request_id=body.get("id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            duration_ms=milliseconds(started),
            outcome="ok",
        )
        return response


# The four ways to total the ledger. The group names come from this table and
# never from the caller, so the query is built from a name the code defines.
GROUPINGS = {
    "day": "date_trunc('day', called_at)::date::text",
    "model": "coalesce(model, 'unknown')",
    "task": "task",
    "run": "run",
}


# The raw rows, newest first. The caller narrows by any field and continues from
# a cursor, so a page never repeats a row the way an offset over a growing table
# would.
CALLS = """
    SELECT id, called_at, provider, model, request_id, task, session_id, entry_id,
           run, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, duration_ms, outcome, error_type
    FROM model_calls
    WHERE ($1::timestamptz IS NULL OR called_at >= $1)
      AND ($2::timestamptz IS NULL OR called_at < $2)
      AND ($3::text IS NULL OR provider = $3)
      AND ($4::text IS NULL OR model = $4)
      AND ($5::text IS NULL OR task = $5)
      AND ($6::text IS NULL OR run = $6)
      AND ($7::text IS NULL OR session_id = $7)
      AND ($8::text IS NULL OR outcome = $8)
      AND ($9::timestamptz IS NULL OR (called_at, id) < ($9, $10))
    ORDER BY called_at DESC, id DESC
    LIMIT $11
"""


def cursor_of(row: dict) -> str:
    """The cursor that continues after this row."""
    return f"{row['called_at'].isoformat()}|{row['id']}"


def parse_cursor(cursor: str) -> tuple[datetime, int]:
    """The moment and the id a cursor stands for."""
    moment, separator, identifier = cursor.rpartition("|")
    if not separator:
        raise ValueError(f"not a cursor: {cursor!r}")
    return datetime.fromisoformat(moment), int(identifier)


async def calls(
    pool: asyncpg.Pool,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: str | None = None,
    run: str | None = None,
    session_id: str | None = None,
    outcome: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """The raw calls, newest first, narrowed by any field.

    This is the read an agent pulls when it wants the figures themselves rather
    than a total. The cursor is the last row of the previous page, and the row
    comparison keeps the page stable while new calls arrive.
    """
    after = parse_cursor(cursor) if cursor else (None, None)
    found = await pool.fetch(
        CALLS,
        since,
        until,
        provider,
        model,
        task,
        run,
        session_id,
        outcome,
        after[0],
        after[1],
        limit,
    )
    return [dict(row) for row in found]


async def usage(
    pool: asyncpg.Pool,
    *,
    by: str = "model",
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Calls and tokens, totalled by day, by model, by task, or by run."""
    group = GROUPINGS[by]
    query = f"""
        SELECT {group} AS key,
               count(*) AS calls,
               coalesce(sum(input_tokens), 0)::bigint AS input_tokens,
               coalesce(sum(output_tokens), 0)::bigint AS output_tokens
        FROM model_calls
        WHERE ($1::timestamptz IS NULL OR called_at >= $1)
          AND ($2::timestamptz IS NULL OR called_at < $2)
        GROUP BY {group}
        ORDER BY calls DESC
    """
    return [dict(row) for row in await pool.fetch(query, since, until)]
