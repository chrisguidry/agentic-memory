"""The exchanges before a pair's prompt.

A prompt like "yeah do that" reads as an answer to nothing on its own. This
answers for what came before it in the same session, one exchange at a time
and newest first, for a person labelling who needs more of the session than
the pair alone shows.
"""

import asyncpg
from fastapi import APIRouter, Request

from ..window import NOT_PLUMBING
from .pairs import PROMPT_ROW

# One real, human prompt before the anchor moment, ranked backward so page 0
# is the exchange right before it and each later page is one turn earlier.
# `bound` is where that exchange's replies stop: the next prompt closer to
# the anchor, or the anchor itself for page 0, because a reply belongs to
# whichever prompt came directly before it.
EARLIER_PROMPT = f"""
    WITH prompts AS (
        SELECT entry_id, occurred_at,
               row_number() OVER (ORDER BY occurred_at DESC) - 1 AS page,
               coalesce(lag(occurred_at) OVER (ORDER BY occurred_at DESC), $2) AS bound
        FROM logs
        WHERE session_id = $1 AND kind = 'prompt' AND actor = 'human'
          AND entry_id IS NOT NULL AND occurred_at < $2
          AND {NOT_PLUMBING}
    )
    SELECT entry_id, occurred_at, bound
    FROM prompts
    WHERE page = $3
"""

EXCHANGE_REPLIES = """
    SELECT body
    FROM logs
    WHERE session_id = $1 AND kind = 'response' AND actor = 'agent' AND actor_depth = 0
      AND occurred_at >= $2 AND occurred_at < $3
    ORDER BY occurred_at
"""


async def earlier_exchange(
    pool: asyncpg.Pool, *, session_id: str, entry_id: str, page: int = 0
) -> dict | None:
    """One exchange before a prompt: page 0 is the one right before it, page 1 the one
    before that, and so on.

    None comes back once paging runs past the session's start, so the TUI
    knows there is nothing older left to show.
    """
    anchor = await pool.fetchrow(PROMPT_ROW, session_id, entry_id)
    if anchor is None or anchor["occurred_at"] is None:
        return None
    found = await pool.fetchrow(EARLIER_PROMPT, session_id, anchor["occurred_at"], page)
    if found is None:
        return None
    prompt = await pool.fetchrow(PROMPT_ROW, session_id, found["entry_id"])
    replies = await pool.fetch(EXCHANGE_REPLIES, session_id, found["occurred_at"], found["bound"])
    return {
        "entry_id": found["entry_id"],
        "occurred_at": found["occurred_at"].isoformat(),
        "prompt": prompt["body"] if prompt else "",
        "replies": [row["body"] for row in replies],
    }


router = APIRouter()


@router.get("/labels/context")
async def context(request: Request, session_id: str, entry_id: str, page: int = 0) -> dict:
    """One exchange before a prompt, for a person who needs to see more than the pair."""
    found = await earlier_exchange(
        request.app.state.pool, session_id=session_id, entry_id=entry_id, page=page
    )
    return {"exchange": found}
