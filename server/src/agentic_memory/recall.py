"""The turn path: what a session is handed, and the record of it.

A turn starts, the client asks, and this answers in one of two forms. A
session's first ask is handed the top of its scope's list, best first. Every
ask after it is handed only the statements that are about the prompt, found
by `match`, or nothing. Both leave out what the session was already handed.
Each form is one read and one write, because a person is waiting.

The handout is recorded by session so that two things can follow from it. A
statement the session already saw is not sent again, because the injected
text lands in the conversation and stays there. And the outcome flow, when it
is built, joins what a turn cost and whether it worked to what the turn was
handed, which is the only way the ranking learns.
"""

from datetime import UTC, datetime

import asyncpg

from .embed import Embedder
from .match import match
from .memories import ranked
from .settings import Settings

# The most statements one turn is handed. A turn's budget is the model's
# context, and past a few dozen sentences the injection is the conversation.
LIMIT = 50

# The reachable slice, less what this session was already handed. The exclusion
# is in the read rather than in Python, so the read stays one query whatever the
# session has seen.
#
# A turn with no scope is a session outside any project, and it is handed only
# what holds everywhere. That differs from a person reading the whole table from
# no scope, which is what `memories.REACHABLE` answers, so the predicate is its
# own rather than that one.
UNSEEN = """
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at, actor, actor_depth
    FROM memories
    WHERE superseded_by IS NULL
      AND (scope_key IS NULL
           OR scope_key = $1
           OR starts_with($1, scope_key || '/'))
      AND NOT EXISTS (
          SELECT 1 FROM injections i
          WHERE i.session_id = $2 AND memories.id = ANY(i.memory_ids)
      )
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


SEEN_ANYTHING = "SELECT EXISTS (SELECT 1 FROM injections WHERE session_id = $1)"


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
    found = await pool.fetch(UNSEEN, scope_key, session_id)
    chosen = ranked((dict(row) for row in found), moment)[: min(limit, LIMIT)]
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
) -> list[dict]:
    """What this turn is handed: the opening list on a session's first ask, a match after."""
    moment = now or datetime.now(UTC)
    if not await pool.fetchval(SEEN_ANYTHING, session_id):
        return await recall(
            pool,
            session_id=session_id,
            harness=harness,
            scope_key=scope_key,
            limit=settings.recall_session_limit,
            now=moment,
        )
    if not prompt.strip():
        return []
    chosen = await match(
        pool,
        embedder,
        session_id=session_id,
        scope_key=scope_key,
        prompt=prompt,
        limit=settings.recall_prompt_limit,
        margin=settings.recall_margin,
        now=moment,
    )
    await record(
        pool, chosen, session_id=session_id, harness=harness, scope_key=scope_key, moment=moment
    )
    return chosen


async def handed(pool: asyncpg.Pool, *, session_id: str, limit: int = 50) -> list[dict]:
    """What a session was handed, latest turn first."""
    return [dict(row) for row in await pool.fetch(HANDED, session_id, limit)]
