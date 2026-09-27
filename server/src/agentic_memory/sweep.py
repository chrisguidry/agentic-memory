"""Rescheduling the work a failed model call left undone.

The live path is keyed by the entry, so once a task's retries are spent,
nothing touches that key again until someone reads the range by hand. The
ledger's `error` rows name every entry a failed call touched, so a sweep can
start there instead of walking the whole record.

A refusal is not an error. The provider read the request and said no, and
sending it again gets the same no, so a refused entry is never a candidate
here. An entry that keeps erroring past the attempt cap is treated the same
way: something is wrong with it that a retry will not fix, and the sweep
stops offering it back rather than asking the provider the same question
every few minutes for a month.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
from docket import CurrentDocket, Depends, Docket, Perpetual, Shared

from .classify import classify, questions_fingerprint, statement_key, task_key
from .db import store_pool
from .settings import Settings, get_settings
from .synthesize import THRESHOLDS, synthesize

log = logging.getLogger("agentic_memory.sweep")


@dataclass(frozen=True)
class Found:
    """What one candidate query turned up: what is still worth trying, and how
    many more have already failed past the attempt cap and were left alone.
    """

    ready: list[tuple[str, str]]
    abandoned: int


def sorted_by_cap(rows: list[asyncpg.Record], attempts: int) -> Found:
    """Split a candidate query's rows by the attempt cap.

    Every row here already cleared the other conditions: the latest call
    errored and nothing has been read or written for it yet. The cap is the
    only reason left to leave one alone.
    """
    ready = [(row["session_id"], row["entry_id"]) for row in rows if row["attempts"] < attempts]
    return Found(ready=ready, abandoned=len(rows) - len(ready))


# The classify entries to read again: the latest call within the lookback
# ended in an error, and no reading exists under the questions asked now. The
# latest call and the attempt count are both read within the same window the
# candidates come from, so an entry a later success already cleared is never
# offered twice.
UNREAD = """
    WITH latest AS (
        SELECT DISTINCT ON (session_id, entry_id) session_id, entry_id, outcome
        FROM model_calls
        WHERE task = 'classify'
          AND session_id IS NOT NULL AND entry_id IS NOT NULL
          AND called_at >= $1
        ORDER BY session_id, entry_id, called_at DESC
    ),
    attempted AS (
        SELECT session_id, entry_id, count(*) AS attempts
        FROM model_calls
        WHERE task = 'classify' AND outcome = 'error'
          AND session_id IS NOT NULL AND entry_id IS NOT NULL
          AND called_at >= $1
        GROUP BY session_id, entry_id
    )
    SELECT l.session_id, l.entry_id, a.attempts
    FROM latest l
    JOIN attempted a ON a.session_id = l.session_id AND a.entry_id = l.entry_id
    WHERE l.outcome = 'error'
      AND NOT EXISTS (
          SELECT 1 FROM classifications c
          WHERE c.session_id = l.session_id AND c.entry_id = l.entry_id
            AND c.questions_fingerprint = $2
      )
    LIMIT $3
"""


async def unread_prompts(
    pool: asyncpg.Pool, *, since: datetime, sweep_attempts: int, limit: int = 500
) -> Found:
    """The prompts a failed classify call left unread, under the attempt cap."""
    found = await pool.fetch(UNREAD, since, questions_fingerprint(), limit)
    return sorted_by_cap(found, sweep_attempts)


# The synthesize entries to write again: the latest call within the lookback
# ended in an error, the reading it was for cleared a kind's threshold, and no
# statement exists under the questions asked now.
UNWRITTEN = f"""
    WITH latest AS (
        SELECT DISTINCT ON (session_id, entry_id) session_id, entry_id, outcome
        FROM model_calls
        WHERE task = 'synthesize'
          AND session_id IS NOT NULL AND entry_id IS NOT NULL
          AND called_at >= $1
        ORDER BY session_id, entry_id, called_at DESC
    ),
    attempted AS (
        SELECT session_id, entry_id, count(*) AS attempts
        FROM model_calls
        WHERE task = 'synthesize' AND outcome = 'error'
          AND session_id IS NOT NULL AND entry_id IS NOT NULL
          AND called_at >= $1
        GROUP BY session_id, entry_id
    )
    SELECT c.session_id, c.entry_id, a.attempts
    FROM latest l
    JOIN attempted a ON a.session_id = l.session_id AND a.entry_id = l.entry_id
    JOIN classifications c
      ON c.session_id = l.session_id AND c.entry_id = l.entry_id
     AND c.questions_fingerprint = $2
    WHERE l.outcome = 'error'
      AND ({" OR ".join(f"c.{kind} >= ${n}" for n, kind in enumerate(THRESHOLDS, start=3))})
      AND NOT EXISTS (
          SELECT 1 FROM memories m
          WHERE m.session_id = c.session_id AND m.entry_id = c.entry_id
            AND m.questions_fingerprint = c.questions_fingerprint
      )
    LIMIT ${len(THRESHOLDS) + 3}
"""


async def unwritten_readings(
    pool: asyncpg.Pool, *, since: datetime, sweep_attempts: int, limit: int = 500
) -> Found:
    """The readings a failed synthesize call left unwritten, under the attempt cap."""
    found = await pool.fetch(UNWRITTEN, since, questions_fingerprint(), *THRESHOLDS.values(), limit)
    return sorted_by_cap(found, sweep_attempts)


async def sweep_failures(
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    docket: Docket = CurrentDocket(),
    now: datetime | None = None,
    perpetual: Perpetual = Perpetual(every=get_settings().sweep_interval, automatic=True),
) -> None:
    """Reschedule the entries a failed call left behind, under the run `sweep`.

    Each task goes back on the same key the live path uses, so a sweep that
    meets a task already scheduled or running writes nothing twice. One
    warning names how many entries the attempt cap left alone this run, rather
    than a warning for each.
    """
    since = (now or datetime.now(UTC)) - settings.sweep_lookback

    unread = await unread_prompts(pool, since=since, sweep_attempts=settings.sweep_attempts)
    for session_id, entry_id in unread.ready:
        try:
            await docket.add(classify, key=task_key(session_id, entry_id))(
                session_id, entry_id, "sweep"
            )
        except Exception:
            log.warning("could not reschedule %s %s", session_id, entry_id, exc_info=True)

    unwritten = await unwritten_readings(pool, since=since, sweep_attempts=settings.sweep_attempts)
    for session_id, entry_id in unwritten.ready:
        try:
            await docket.add(synthesize, key=statement_key(session_id, entry_id))(
                session_id, entry_id, "sweep"
            )
        except Exception:
            log.warning("could not reschedule %s %s", session_id, entry_id, exc_info=True)

    abandoned = unread.abandoned + unwritten.abandoned
    if abandoned:
        log.warning("%s entries have failed past the attempt cap and were left alone", abandoned)
