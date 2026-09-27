"""Running the record's prompts through the turn path at the moment each was said."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from ..embed import Embedder
from ..recall import Handout, choose
from ..settings import Settings
from ..window import NOT_PLUMBING

# The prompts the classifier reads, in the order they were said. A harness
# writes its own entries as prompts, and the classifier's filter drops them, so
# a replay is asked about the same messages the person typed.
#
# A prompt that arrived after the moment is left out, and so is one said after
# it. With both cuts, a replay run later against a store that has taken more
# records reads the same prompts.
PROMPTS = f"""
    SELECT session_id, entry_id, harness, scope_key, occurred_at, body
    FROM logs
    WHERE kind = 'prompt'
      AND occurred_at >= $1 AND occurred_at < least($2::timestamptz, $3::timestamptz)
      AND received_at <= $3
      AND session_id IS NOT NULL
      AND entry_id IS NOT NULL
      AND {NOT_PLUMBING}
    ORDER BY occurred_at, id
"""


@dataclass(frozen=True)
class Prompt:
    """One prompt from the record, and where and when it was said."""

    session_id: str
    entry_id: str
    harness: str | None
    scope_key: str | None
    said_at: datetime
    body: str


@dataclass(frozen=True)
class Turn:
    """One replayed prompt, and what the turn path handed it."""

    prompt: Prompt
    handout: Handout


async def prompts(
    pool: asyncpg.Pool, *, since: datetime, until: datetime, as_of: datetime
) -> list[Prompt]:
    """The prompts said in a range, as the record held them at a moment."""
    found = await pool.fetch(PROMPTS, since, until, as_of)
    return [
        Prompt(
            session_id=row["session_id"],
            entry_id=row["entry_id"],
            harness=row["harness"],
            scope_key=row["scope_key"],
            said_at=row["occurred_at"],
            body=row["body"] or "",
        )
        for row in found
    ]


async def replay(
    pool: asyncpg.Pool, embedder: Embedder, settings: Settings, said: Sequence[Prompt]
) -> list[Turn]:
    """What each prompt would be handed, in the order the prompts were said.

    Each prompt reads the table as it stood when the prompt was said, and ages
    the statements to that moment. What a session was handed is kept here and
    not in the injections table, so the opening list and the filter on what a
    session saw work as they did live, and the table is not written.
    """
    handed: dict[str, set[int]] = defaultdict(set)
    turns = []
    for prompt in said:
        seen = handed[prompt.session_id]
        handout = await choose(
            pool,
            embedder,
            settings,
            seen=frozenset(seen),
            scope_key=prompt.scope_key,
            prompt=prompt.body,
            now=prompt.said_at,
            as_of=prompt.said_at,
        )
        seen.update(row["id"] for row in handout.statements)
        turns.append(Turn(prompt, handout))
    return turns
