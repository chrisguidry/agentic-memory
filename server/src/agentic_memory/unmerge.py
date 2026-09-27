"""Undoing what one merge run retired.

A merge retires a statement through `superseded_by` and deletes nothing, so a
run whose merges were wrong is undone by clearing the pointer on the statements
it retired. Its comparison marks are cleared too, so a rerun under better rules
asks about the same pairs again rather than passing them as already compared.

The merge records its run on each statement it retires and on each statement it
compares, and an undo by run restores each retirement of the run whose
survivor's text differs from its own. A survivor with the same text is a
duplicate, and its merge is right under any rules.

A retirement made before the merge recorded its run has no run on the row, and
a window of time finds it instead: every such retirement in the window whose
survivor's text differs from its own. The window also finds a correction or a
sort made in it, because those record no run either, so a window has an end as
well as a start, and a dry run shows what it would restore.
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg

# The runs the writer merges under, after each statement it writes: `live` for
# every message, `write` and `reread` for the routes of those names, and
# `sweep` for a write the sweep retries. Undoing one of them would restore every
# statement the writer retired under it, so they are refused.
WRITERS = frozenset({"live", "write", "reread", "sweep"})

# The `/write` and `/reread` routes take a run of any name, so a name alone
# cannot tell a writer's run from a merge pass. The ledger can: the writer
# classifies and writes under its run, and a merge pass makes neither call.
WRITTEN_UNDER = """
    SELECT EXISTS (
        SELECT 1 FROM model_calls WHERE run = $1 AND task IN ('classify', 'synthesize')
    )
"""

# A survivor with the same text as the statement it retired is a duplicate,
# and its merge is right under any rules, so it stays retired.
RESTORE = """
    UPDATE memories m
    SET superseded_by = NULL, superseded_at = NULL, retired_by_run = NULL
    FROM memories s
    WHERE s.id = m.superseded_by
      AND m.retired_by_run = $1
      AND s.statement <> m.statement
    RETURNING m.id
"""

REOPEN = """
    UPDATE memories
    SET merged_at = NULL, merged_by_run = NULL
    WHERE merged_by_run = $1
    RETURNING id
"""

# `$1` and `$2` are the start and the end of the window, and the end is not in
# it.
RESTORE_IN_WINDOW = """
    UPDATE memories m
    SET superseded_by = NULL, superseded_at = NULL
    FROM memories s
    WHERE s.id = m.superseded_by
      AND m.retired_by_run IS NULL
      AND m.superseded_at >= $1
      AND m.superseded_at < $2
      AND s.statement <> m.statement
    RETURNING m.id
"""

REOPEN_IN_WINDOW = """
    UPDATE memories
    SET merged_at = NULL
    WHERE merged_by_run IS NULL
      AND merged_at >= $1
      AND merged_at < $2
    RETURNING id
"""


class Refused(ValueError):
    """A run or a window the undo will not touch."""


@dataclass(frozen=True)
class Undone:
    """The statements that are live again, and the ones that will be compared again."""

    restored: list[int]
    reopened: list[int]


async def changed(connection: asyncpg.Connection, query: str, *values) -> list[int]:
    return sorted(row["id"] for row in await connection.fetch(query, *values))


async def refuse(connection: asyncpg.Connection, run: str) -> None:
    """Refuse a run the writer merged under, by its name or by its calls."""
    if run in WRITERS or await connection.fetchval(WRITTEN_UNDER, run):
        raise Refused(
            f"{run} is a run the writer merges under, and undoing it would restore every"
            " statement the writer retired under it. The refused runs are"
            f" {', '.join(sorted(WRITERS))}, and any run with a classify or synthesize"
            " call in the ledger."
        )


def window_of(since: datetime | None, until: datetime | None) -> tuple[datetime, datetime] | None:
    """The window from `since` up to `until`, or none when neither is given.

    A window with one end would reach to the start or the end of the table,
    and a window that ends before it starts is a mistake in the order of the
    two, so both are refused rather than read as something else.
    """
    if since is None and until is None:
        return None
    if since is None or until is None:
        raise Refused(
            "since and until are the start and the end of one window, so each needs the other"
        )
    if since > until:
        raise Refused(f"the window starts at {since}, after its end at {until}")
    return since, until


async def undo(
    pool: asyncpg.Pool,
    *,
    run: str,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
) -> Undone:
    """Restore what the run retired, and clear what it compared.

    From `since` up to `until`, the retirements and the comparison marks that
    record no run are undone too. Every change is one transaction, so an undo
    that fails leaves the table as it was, and a dry run rolls the transaction
    back after it has counted.
    """
    window = window_of(since, until)
    async with pool.acquire() as connection:
        await refuse(connection, run)
        transaction = connection.transaction()
        await transaction.start()
        try:
            restored = await changed(connection, RESTORE, run)
            reopened = await changed(connection, REOPEN, run)
            if window is not None:
                restored += await changed(connection, RESTORE_IN_WINDOW, *window)
                reopened += await changed(connection, REOPEN_IN_WINDOW, *window)
        except BaseException:
            await transaction.rollback()
            raise
        if dry_run:
            await transaction.rollback()
        else:
            await transaction.commit()
    return Undone(restored, reopened)
