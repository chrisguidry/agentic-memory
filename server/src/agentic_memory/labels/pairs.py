"""Walking a sample and taking a person's judgment of each pair.

`next_unlabelled` is what the TUI polls between key presses: the next pair
nobody has judged yet, with enough of the session around the prompt that a
person can tell what it was answering. `write_label` is what a key press
sends back.
"""

from typing import Literal

import asyncpg
from fastapi import APIRouter, Request
from pydantic import BaseModel

Label = Literal["good", "noise", "wrong"]

# Whether a pair has been judged is asked by joining to `labels` on the triple
# that names a pair, and not by a foreign key from `label_pairs`, because the
# same pair can be drawn into more than one sample and a label belongs to the
# pair rather than to whichever sample happened to draw it.
COUNTS = """
    SELECT count(*) AS total,
           count(*) FILTER (
               WHERE EXISTS (
                   SELECT 1 FROM labels l
                   WHERE l.session_id = lp.session_id AND l.entry_id = lp.entry_id
                     AND l.memory_id = lp.memory_id
               )
           ) AS done
    FROM label_pairs lp
    WHERE lp.sample = $1
"""

NEXT_UNLABELLED = """
    SELECT lp.id, lp.session_id, lp.entry_id, lp.memory_id
    FROM label_pairs lp
    WHERE lp.sample = $1 AND lp.id > $2
      AND NOT EXISTS (
          SELECT 1 FROM labels l
          WHERE l.session_id = lp.session_id AND l.entry_id = lp.entry_id
            AND l.memory_id = lp.memory_id
      )
    ORDER BY lp.id
    LIMIT 1
"""

# The prompt row a pair points at, and what `context` reads to anchor its own
# paging: the body, where it was said, and when.
PROMPT_ROW = """
    SELECT body, working_directory, occurred_at
    FROM logs
    WHERE session_id = $1 AND entry_id = $2 AND kind = 'prompt'
    LIMIT 1
"""

STATEMENT = "SELECT statement, kind, scope_key, said_at FROM memories WHERE id = $1"

# The scope of the session the prompt was said in, read off whichever
# injection actually handed this memory to it. A session's scope does not
# move turn to turn, but reading it from the injection rather than the
# session keeps this tied to the handout being judged, not a guess about it.
SESSION_SCOPE = """
    SELECT scope_key
    FROM injections
    WHERE session_id = $1 AND memory_ids @> ARRAY[$2]::bigint[]
    ORDER BY injected_at
    LIMIT 1
"""

# The agent's own words just before the prompt, so a terse prompt like "yeah
# do that" reads as an answer to something instead of nothing. Depth zero,
# because a subagent's reply is not what the person was looking at.
LAST_REPLY = """
    SELECT body
    FROM logs
    WHERE session_id = $1 AND kind = 'response' AND actor = 'agent' AND actor_depth = 0
      AND occurred_at < $2
    ORDER BY occurred_at DESC
    LIMIT 1
"""


async def next_unlabelled(pool: asyncpg.Pool, *, sample: str, after: int = 0) -> dict:
    """The next pair of a sample nobody has judged, and how far the sample has come.

    A pair already labelled under another sample counts as done here too: the
    label belongs to the pair, not to the sample that happened to draw it.

    `after` moves past a pair without judging it, and once every later pair is
    spoken for it wraps back to the start, so skipping revisits what was
    skipped instead of losing it.
    """
    counts = await pool.fetchrow(COUNTS, sample)
    found = await pool.fetchrow(NEXT_UNLABELLED, sample, after)
    if found is None and after:
        found = await pool.fetchrow(NEXT_UNLABELLED, sample, 0)
    if found is None:
        return {"done": counts["done"], "total": counts["total"], "pair": None}

    prompt = await pool.fetchrow(PROMPT_ROW, found["session_id"], found["entry_id"])
    statement = await pool.fetchrow(STATEMENT, found["memory_id"])
    scope = await pool.fetchval(SESSION_SCOPE, found["session_id"], found["memory_id"])
    last_reply = None
    if prompt and prompt["occurred_at"] is not None:
        last_reply = await pool.fetchval(LAST_REPLY, found["session_id"], prompt["occurred_at"])

    return {
        "done": counts["done"],
        "total": counts["total"],
        "pair": {
            "id": found["id"],
            "session_id": found["session_id"],
            "entry_id": found["entry_id"],
            "memory_id": found["memory_id"],
            "prompt": prompt["body"] if prompt else "",
            "occurred_at": prompt["occurred_at"].isoformat()
            if prompt and prompt["occurred_at"]
            else None,
            "working_directory": prompt["working_directory"] if prompt else None,
            "session_scope_key": scope,
            "last_reply": last_reply,
            "statement": statement["statement"] if statement else "",
            "kind": statement["kind"] if statement else None,
            "scope_key": statement["scope_key"] if statement else None,
            "said_at": statement["said_at"].isoformat()
            if statement and statement["said_at"]
            else None,
        },
    }


WRITE_LABEL = """
    INSERT INTO labels (session_id, entry_id, memory_id, label, labelled_at)
    VALUES ($1, $2, $3, $4, now())
    ON CONFLICT (session_id, entry_id, memory_id)
    DO UPDATE SET label = EXCLUDED.label, labelled_at = EXCLUDED.labelled_at
"""


async def write_label(
    pool: asyncpg.Pool, *, session_id: str, entry_id: str, memory_id: int, label: Label
) -> None:
    """Judge one pair, replacing whatever this pair was judged before."""
    await pool.execute(WRITE_LABEL, session_id, entry_id, memory_id, label)


router = APIRouter()


class Judgment(BaseModel):
    """What the TUI sends after a key press."""

    session_id: str
    entry_id: str
    memory_id: int
    label: Label


@router.get("/labels/next")
async def next_pair(request: Request, sample: str, after: int = 0) -> dict:
    """The next pair of a sample nobody has judged, and how far it has come."""
    return await next_unlabelled(request.app.state.pool, sample=sample, after=after)


@router.post("/labels")
async def label_pair(request: Request, judgment: Judgment) -> dict:
    """Judge one pair. A second judgment of the same pair replaces the first."""
    await write_label(
        request.app.state.pool,
        session_id=judgment.session_id,
        entry_id=judgment.entry_id,
        memory_id=judgment.memory_id,
        label=judgment.label,
    )
    return {"labelled": judgment.label}
