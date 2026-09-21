"""The turn path: what a session is handed, and the record of it.

A turn starts, the client asks, and this answers with the live statements
reachable from where the session is, best first, less whatever the session
was already handed. It is one read and one write, and it calls no model,
because a person is waiting.

The handout is recorded by session so that two things can follow from it. A
statement the session already saw is not sent again, because the injected
text lands in the conversation and stays there. And the outcome flow, when it
is built, joins what a turn cost and whether it worked to what the turn was
handed, which is the only way the ranking learns.
"""

from datetime import UTC, datetime

import asyncpg

from .memories import ranked

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


async def recall(
    pool: asyncpg.Pool,
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    limit: int = LIMIT,
    now: datetime | None = None,
) -> list[dict]:
    """The statements a turn is handed, best first, and the record that it was."""
    moment = now or datetime.now(UTC)
    found = await pool.fetch(UNSEEN, scope_key, session_id)
    chosen = ranked((dict(row) for row in found), moment)[: min(limit, LIMIT)]
    if chosen:
        await pool.execute(
            RECORD, session_id, harness, scope_key, [row["id"] for row in chosen], moment
        )
    return chosen


async def handed(pool: asyncpg.Pool, *, session_id: str, limit: int = 50) -> list[dict]:
    """What a session was handed, latest turn first."""
    return [dict(row) for row in await pool.fetch(HANDED, session_id, limit)]
