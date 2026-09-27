"""The merge pass over every live statement already in the table.

A statement is merged as it is written. This pass is for what was written
before the merge existed, for a model change that re-embeds the table, and for
a change to the merge's questions. It takes every live statement oldest first,
and each one meets the live statements said before it, so each pair is asked
about once and the survivor of a group is its newest statement.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg
from docket import Depends, Shared

from .classify import recorded_model_client
from .db import store_pool
from .ledger import RecordedSystemOne
from .memories import live
from .merge import Merged, merge
from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.merge_pass")

# The run a pass records its calls under when it is given no name, so its cost
# totals apart from the merges the writer makes on each write.
RUN = "merge"


class Throttled:
    """A System One client that makes at most `limit` calls at once."""

    def __init__(self, client: RecordedSystemOne, limit: int) -> None:
        self.client = client
        self.slots = asyncio.Semaphore(limit)

    async def system_one(self, *, state: Any, questions: Any) -> Any:
        async with self.slots:
            return await self.client.system_one(state=state, questions=questions)


# Every live statement of this model that could still be merged, oldest first,
# narrowed to one scope and to the oldest few when those are given. Oldest first
# means each statement meets the ones said before it, so the survivor of a group
# is its newest statement. `$4` takes the statements already compared as well,
# which is how a change to the questions is applied to the whole table.
BACKLOG = f"""
    SELECT m.id, m.scope_key
    FROM memories m
    WHERE {live("m")}
      AND (m.until_moment IS NULL OR m.until_moment > now())
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $1
      AND m.said_at IS NOT NULL
      AND ($2::text IS NULL OR m.scope_key = $2)
      AND ($4::boolean OR m.merged_at IS NULL)
    ORDER BY m.said_at ASC, m.id ASC
    LIMIT $3
"""


@dataclass(frozen=True)
class Pass:
    """What a pass retired, and the statements it could not compare."""

    merged: list[Merged]
    failed: list[int]


async def merge_backlog(
    *,
    settings: Settings,
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    run: str = RUN,
    scope: str | None = None,
    limit: int | None = None,
    concurrency: int = 1,
    again: bool = False,
) -> Pass:
    """Merge every live statement with the ones said before it, oldest first.

    This is the pass that clears what is already in the table, and with `again`
    the one that applies a change to the questions. A statement meets only
    statements in its own scope, so the scopes run side by side, and each scope
    runs in order.

    A statement whose comparison fails is logged and left unmarked, and the pass
    goes on to the next one. A pass of thousands of calls meets a timeout or a
    dropped connection, and stopping there would leave the rest of the table
    unmerged. A rerun takes only the unmarked statements, so it pays for the
    ones that failed and not again for the ones that did not.
    """
    found = await pool.fetch(BACKLOG, settings.embed_model, scope, limit, again)
    scopes: dict[str | None, list[int]] = {}
    for row in found:
        scopes.setdefault(row["scope_key"], []).append(row["id"])

    judge = Throttled(client, concurrency)
    merged: list[Merged] = []
    failed: list[int] = []

    async def in_order(statements: list[int]) -> None:
        for statement_id in statements:
            try:
                merged.extend(
                    await merge(
                        pool,
                        judge,
                        statement_id=statement_id,
                        model=settings.embed_model,
                        settings=settings,
                        run=run,
                        earlier=True,
                    )
                )
            except Exception:
                log.warning("could not merge statement %s", statement_id, exc_info=True)
                failed.append(statement_id)

    await asyncio.gather(*map(in_order, scopes.values()))
    log.info(
        "merged %s of %s statements over the backlog, and %s failed",
        len(merged),
        len(found),
        len(failed),
    )
    return Pass(merged, failed)


# How many statements the pass would take, and how many pairs in the band they
# make with the live statements said before them, which is where the model is
# asked. The pass retires statements as it goes, and a retired statement is not
# asked about again, so the pairs are the most the pass can ask.
BAND = f"""
    WITH subjects AS ({BACKLOG})
    SELECT (SELECT count(*) FROM subjects) AS statements,
           (SELECT count(*)
            FROM subjects s
            JOIN memories a ON a.id = s.id
            JOIN memories b ON b.scope_key IS NOT DISTINCT FROM a.scope_key
            WHERE {live("b")}
              AND (b.until_moment IS NULL OR b.until_moment > now())
              AND b.embedding IS NOT NULL
              AND b.embedding_model = $1
              AND b.said_at IS NOT NULL
              AND (b.said_at, b.id) < (a.said_at, a.id)
              AND 1 - (a.embedding <=> b.embedding) >= $5
              AND 1 - (a.embedding <=> b.embedding) < $6
              AND ($7::boolean OR a.kind = b.kind)) AS pairs
"""

# The tokens of a merge call, from the calls of the run being priced only. The
# questions change between runs, and a merge call made before the second
# question existed was a smaller call, so a run is priced from a small first
# run under its own name.
SPENT = """
    SELECT avg(input_tokens) AS input_tokens, avg(output_tokens) AS output_tokens
    FROM model_calls
    WHERE task = 'merge' AND outcome = 'ok' AND run = $1
"""


@dataclass(frozen=True)
class Price:
    """What a pass would take: its statements, its pairs, and its tokens when known."""

    statements: int
    pairs: int
    tokens: tuple[int, int] | None


async def price(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    run: str,
    scope: str | None = None,
    limit: int | None = None,
    again: bool = False,
) -> Price:
    """The statements a pass would take, the pairs it could ask, and their tokens."""
    band = await pool.fetchrow(
        BAND,
        settings.embed_model,
        scope,
        limit,
        again,
        settings.merge_lower,
        settings.merge_upper,
        settings.merge_across_kinds,
    )
    spent = await pool.fetchrow(SPENT, run)
    tokens = None
    if spent["input_tokens"] is not None:
        tokens = (
            round(spent["input_tokens"] * band["pairs"]),
            round(spent["output_tokens"] * band["pairs"]),
        )
    return Price(band["statements"], band["pairs"], tokens)


async def merge_statements(
    run: str = RUN,
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    client: RecordedSystemOne = Shared(recorded_model_client),
) -> None:
    """Merge the statements not yet compared, as a task."""
    await merge_backlog(settings=settings, pool=pool, client=client, run=run)
