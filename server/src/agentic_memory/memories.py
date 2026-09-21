"""What a statement means once it has been written.

The writer fills the table. This says what a statement is worth reading now,
and how a statement leaves the list when a newer one replaces it.

A statement is retired rather than deleted or edited. The statement that
replaced it is named on the row, so a question about the past still has an
answer, and the chain of replacements is the reason the service believes what
it believes.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import asyncpg

log = logging.getLogger("agentic_memory.memories")

# How much a kind of statement is worth on its own, how long it takes to lose
# half of that to age, and the part of it that age never takes away.
#
# A floor is for the kinds the record gives no reason to think have stopped
# being true. Decaying one of those to nothing would bury a rule that has held
# for a year, and a person who writes the same rule into every session is
# saying that age is not the question. The kinds without a floor expire on
# their own: a plan describes work in flight, and praise is about an outcome
# that is already past.
#
# These numbers are guesses. The outcome flow is the only thing that can set
# them honestly, and it is not built.
RANKING: dict[str, tuple[float, float, float]] = {
    # kind: weight, half-life in days, floor
    "preference": (1.0, 90.0, 0.6),
    "correction": (1.0, 90.0, 0.6),
    "semantic": (0.9, 90.0, 0.6),
    "procedural": (0.9, 90.0, 0.6),
    "praise": (0.5, 30.0, 0.0),
    "prospective": (0.4, 14.0, 0.0),
}

# A kind with no entry ranks below every kind that has one, because a kind
# nobody has given a weight to is a kind nobody has decided about.
UNRANKED = (0.1, 90.0, 0.0)

# How many statements one message is offered to replace. The newest are offered
# first, so a statement older than the cap cannot be retired by the message, and
# the cap is logged when it is reached rather than dropping a statement in
# silence.
CANDIDATES = 40

# A statement and everything above it. The scope is a path, so a statement about
# an organization is reachable from a repository inside it, and a statement with
# no scope is reachable from everywhere.
REACHABLE = """
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at
    FROM memories
    WHERE superseded_by IS NULL
      AND ($1::text IS NULL
           OR scope_key IS NULL
           OR scope_key = $1
           OR $1 LIKE scope_key || '%')
"""

# The statements a message could replace: the live ones reachable from where it
# was said, of the kinds that fired, newest first.
#
# A statement can only be retired by a message that came after the one it was
# written from. Without that rule, a re-read of last year, which writes old
# statements today, could retire a statement written from this morning.
STANDING = (
    REACHABLE
    + """
      AND kind = ANY($2::text[])
      AND said_at IS NOT NULL
      AND said_at < $3
    ORDER BY said_at DESC
    LIMIT $4
"""
)

READ = (
    REACHABLE
    + """
    ORDER BY said_at DESC NULLS LAST, created_at DESC
"""
)

RETIRE = """
    UPDATE memories
    SET superseded_by = $2, superseded_at = $3
    WHERE id = $1 AND superseded_by IS NULL
"""


def worth(row: dict[str, Any], now: datetime | None = None) -> float:
    """What a statement is worth reading now.

    The classifier's probability is not part of this. It answers whether the
    message held a memory of that kind, which is the question that got the
    statement written, and once that is settled it says nothing about whether
    the statement belongs in front of a session today.

    The age is measured from when the message was said, not from when the
    statement was written. A backfill writes a year of statements in a minute,
    and the recorded time would then rank a statement from last winter as
    though it were new.
    """
    weight, half_life, floor = RANKING.get(row["kind"], UNRANKED)
    moment = now or datetime.now(UTC)
    said_at = row.get("said_at") or row["created_at"]
    age_in_days = max((moment - said_at).total_seconds(), 0.0) / 86400
    return weight * (floor + (1 - floor) * 0.5 ** (age_in_days / half_life))


def ranked(rows: Iterable[dict[str, Any]], now: datetime | None = None) -> list[dict]:
    """The statements in the order a scope reads them, best first."""
    moment = now or datetime.now(UTC)
    scored = [{**row, "rank": worth(row, moment)} for row in rows]
    return sorted(scored, key=lambda row: (row["rank"], row["created_at"]), reverse=True)


async def standing(
    pool: asyncpg.Pool,
    *,
    scope_key: str | None,
    kinds: Iterable[str],
    said_before: datetime | None,
    limit: int = CANDIDATES,
) -> list[dict]:
    """What a message said here could replace.

    Only a message that pushes back on something is asked this, and the cuts
    that make the answer small are the scope, the kind, and that gate. The
    comparison is then against single digits of statements rather than the whole
    table, and it costs more tokens on a call that was already being made
    instead of a call of its own.

    Nothing is offered when the message itself has no known time, because a
    statement that cannot be placed in order cannot be replaced in order.
    """
    if said_before is None:
        log.info("no time for the message, so nothing is offered to a message in %s", scope_key)
        return []
    found = await pool.fetch(STANDING, scope_key, list(kinds), said_before, limit)
    if len(found) == limit:
        log.info(
            "offered %s statements to replace in %s, and older ones were not offered",
            limit,
            scope_key or "everywhere",
        )
    return [dict(row) for row in found]


async def memories(
    pool: asyncpg.Pool,
    *,
    scope_key: str | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[dict]:
    """What is worth remembering for a place, best first.

    A statement is reachable from a scope when it is scoped to that scope, to
    any scope above it, or to none, so nothing has to be declared for a
    statement about a repository to reach a directory inside it.
    """
    found = await pool.fetch(READ, scope_key)
    return ranked((dict(row) for row in found), now)[:limit]


async def retire(
    pool: asyncpg.Pool,
    *,
    replaced: Iterable[int],
    replacement: int,
    now: datetime | None = None,
) -> list[int]:
    """End the statements a newer one replaces, and say which ones ended.

    The moment recorded is when the service learned about the replacement, and
    not the end of the interval the replaced statement was true in. The service
    learns that something stopped being true at a different moment from when it
    stopped, and the two are kept apart on purpose.
    """
    moment = now or datetime.now(UTC)
    ended = []
    for statement_id in replaced:
        if statement_id == replacement:
            continue
        result = await pool.execute(RETIRE, statement_id, replacement, moment)
        if result.endswith(" 1"):
            ended.append(statement_id)
    return ended
