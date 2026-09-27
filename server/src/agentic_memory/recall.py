"""The turn path: what a session is handed, and the record of it.

A turn starts, the client asks, and this answers in one of two forms. A
session's first ask is handed the top of its scope's list, best first. Every
ask after it is handed only the statements that are about the prompt, found
by `match`, or nothing. Both leave out what the session was already handed.
Each form is two reads and one write, because a person is waiting.

The handout is recorded by session so that two things can follow from it. A
statement the session already saw is not sent again, because the injected
text lands in the conversation and stays there. And the outcome flow, when it
is built, joins what a turn cost and whether it worked to what the turn was
handed, which is the only way the ranking learns.
"""

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import asyncpg

from .embed import Embedder
from .match import match
from .memories import live_at, ranked
from .metrics import RECALLS, phase
from .settings import Settings

# The most statements one turn is handed. A turn's budget is the model's
# context, and past a few dozen sentences the injection is the conversation.
LIMIT = 50

# Every statement a session was handed. The turn path reads this once and
# passes it to the query that chooses, so the same query serves two sources of
# the list: the injections table for a live turn, and the replay's own record
# for a replayed one.
SEEN = """
    SELECT coalesce(array_agg(DISTINCT handed.id), '{}')
    FROM injections, unnest(memory_ids) AS handed(id)
    WHERE session_id = $1
"""

# The reachable slice at a moment, less what this session was already handed.
#
# A turn with no scope is a session outside any project, and it is handed only
# what holds everywhere. That differs from a person reading the whole table from
# no scope, which is what `memories.REACHABLE` answers, so the predicate is its
# own rather than that one.
UNSEEN = f"""
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at, actor, actor_depth
    FROM memories
    WHERE {live_at("$3")}
      AND (scope_key IS NULL
           OR scope_key = $1
           OR starts_with($1, scope_key || '/'))
      AND NOT (id = ANY($2::bigint[]))
"""

RECORD = """
    INSERT INTO injections (session_id, harness, scope_key, memory_ids, injected_at)
    VALUES ($1, $2, $3, $4::bigint[], $5)
"""

HANDED = """
    SELECT id, session_id, harness, scope_key, memory_ids, injected_at
    FROM injections
    WHERE session_id = $1
    ORDER BY injected_at DESC
    LIMIT $2
"""

# The two forms a turn takes. `opening` is the top of the scope's list, for a
# session that has been handed nothing yet, and `match` is the statements about
# the prompt, for every ask after that.
Form = Literal["opening", "match"]


@dataclass(frozen=True)
class Handout:
    """What one turn is handed, and the form that chose it."""

    form: Form
    statements: list[dict]


def form_for(seen: Collection[int]) -> Form:
    """The form a turn takes, from what its session was handed before."""
    return "match" if seen else "opening"


async def seen_by(pool: asyncpg.Pool, session_id: str) -> frozenset[int]:
    """Every statement this session was handed."""
    return frozenset(await pool.fetchval(SEEN, session_id))


async def opening(
    pool: asyncpg.Pool,
    *,
    scope_key: str | None,
    seen: Collection[int],
    limit: int,
    now: datetime,
    as_of: datetime | None = None,
) -> list[dict]:
    """The top of the scope's list at a moment, less what the session saw."""
    with phase("opening"):
        found = await pool.fetch(UNSEEN, scope_key, list(seen), as_of)
    return ranked((dict(row) for row in found), now)[: min(limit, LIMIT)]


async def choose(
    pool: asyncpg.Pool,
    embedder: Embedder,
    settings: Settings,
    *,
    seen: Collection[int],
    scope_key: str | None,
    prompt: str,
    now: datetime,
    as_of: datetime | None = None,
) -> Handout:
    """What a turn is handed, chosen with nothing recorded.

    `now` is the moment the statements are aged to, and `as_of` is the moment
    the table is read at. A live turn reads the table as it is. A replay reads
    it as it stood when the prompt was said.
    """
    form = form_for(seen)
    if form == "opening":
        chosen = await opening(
            pool,
            scope_key=scope_key,
            seen=seen,
            limit=settings.recall_session_limit,
            now=now,
            as_of=as_of,
        )
        return Handout(form, chosen)
    if not prompt.strip():
        return Handout(form, [])
    chosen = await match(
        pool,
        embedder,
        seen=seen,
        scope_key=scope_key,
        prompt=prompt,
        limit=settings.recall_prompt_limit,
        margin=settings.recall_margin,
        now=now,
        as_of=as_of,
    )
    return Handout(form, chosen)


async def record(
    pool: asyncpg.Pool,
    chosen: list[dict],
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    moment: datetime,
) -> None:
    """Write down what went, when anything did."""
    if chosen:
        with phase("record"):
            await pool.execute(
                RECORD, session_id, harness, scope_key, [row["id"] for row in chosen], moment
            )


async def recall(
    pool: asyncpg.Pool,
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    limit: int = LIMIT,
    now: datetime | None = None,
) -> list[dict]:
    """The top of the scope's list, less what the session saw, and the record of it."""
    moment = now or datetime.now(UTC)
    chosen = await opening(
        pool, scope_key=scope_key, seen=await seen_by(pool, session_id), limit=limit, now=moment
    )
    await record(
        pool, chosen, session_id=session_id, harness=harness, scope_key=scope_key, moment=moment
    )
    return chosen


async def turn(
    pool: asyncpg.Pool,
    embedder: Embedder,
    settings: Settings,
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    prompt: str,
    now: datetime | None = None,
    waiting: Callable[[], Awaitable[bool]] | None = None,
) -> Handout:
    """What this turn is handed: the opening list on a session's first ask, a match after.

    `waiting` says whether the client is still waiting for the answer. A
    statement recorded for a client that stopped waiting never reached the
    session, and the record would keep the session from ever being handed it,
    so a handout is recorded only while the client waits.
    """
    moment = now or datetime.now(UTC)
    # A recall that fails before it reads what the session was handed has no
    # form yet, and its error is counted under this one.
    form = "unknown"
    try:
        with phase("seen"):
            handed_before = await seen_by(pool, session_id)
        form = form_for(handed_before)
        handout = await choose(
            pool,
            embedder,
            settings,
            seen=handed_before,
            scope_key=scope_key,
            prompt=prompt,
            now=moment,
        )
        # An empty handout writes nothing, so the check on the client runs only
        # when a write would follow.
        if handout.statements and waiting is not None and not await waiting():
            RECALLS.labels(form=form, outcome="gone").inc()
            return handout
        await record(
            pool,
            handout.statements,
            session_id=session_id,
            harness=harness,
            scope_key=scope_key,
            moment=moment,
        )
    except Exception:
        RECALLS.labels(form=form, outcome="error").inc()
        raise
    RECALLS.labels(form=form, outcome="handed" if handout.statements else "nothing").inc()
    return handout


async def handed(pool: asyncpg.Pool, *, session_id: str, limit: int = 50) -> list[dict]:
    """What a session was handed, latest turn first."""
    return [dict(row) for row in await pool.fetch(HANDED, session_id, limit)]
