"""The statements that are about a prompt.

The prompt's embedding is compared with every live statement reachable from
the scope. The few that score well above the rest are handed over, and most
prompts get nothing, because most prompts are about the work in front of the
person and the record has nothing to add.
"""

import math
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .embed import literal
from .memories import live_at, worth
from .metrics import phase

# Every reachable statement this model has embedded that was live at the
# moment, nearest first, with whether the session was already handed it. The
# seen ones are read too, and not filtered here, because the baseline below is
# over the whole scope and must not move as a session is handed more.
NEAREST = f"""
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at, actor, actor_depth,
           actionable, 1 - (embedding <=> $1::vector) AS similarity,
           id = ANY($3::bigint[]) AS seen
    FROM memories
    WHERE {live_at("$5")}
      AND embedding IS NOT NULL
      AND embedding_model = $4
      AND (scope_key IS NULL
           OR scope_key = $2
           OR starts_with($2, scope_key || '/'))
    ORDER BY embedding <=> $1::vector
"""


# How many statements are above the baseline at the least. In a scope of a few
# thousand the ninety-ninth percentile has thirty above it, which is where the
# margin was measured. In a scope of a hundred it would have one, and the
# handful of wordings of one rule would then hide each other, so the baseline
# never rises above the tenth best.
ABOVE_BASELINE = 10


def baseline(similarities: list[float]) -> float | None:
    """The ninety-ninth percentile of similarity over the scope, or the tenth best.

    On a real hit the best statement scores well above this and on a miss it
    does not, whatever its absolute score, which is why the cutoff is a margin
    over it and not a number. A scope with ten statements or fewer has no
    baseline, and its prompts are handed nothing.
    """
    ordered = sorted(similarities, reverse=True)
    above = max(ABOVE_BASELINE, math.ceil(len(ordered) / 100))
    if len(ordered) <= above:
        return None
    return ordered[above]


def clears(row: Mapping[str, Any], threshold: float) -> bool:
    """Whether a statement may be handed out, by its answer to the actionable question.

    A statement with no answer yet is handed out, so a statement written while
    the model was unreachable, or before the question existed, is matched as it
    was before rather than never.
    """
    return row["actionable"] is None or row["actionable"] >= threshold


async def match(
    pool: asyncpg.Pool,
    vector: list[float],
    *,
    model: str,
    seen: Collection[int],
    scope_key: str | None,
    limit: int,
    margin: float,
    actionable: float,
    now: datetime | None = None,
    as_of: datetime | None = None,
) -> list[dict]:
    """The statements about this prompt that the session has not seen, best first.

    `vector` is the prompt embedded by `model`. `actionable` is the threshold a
    statement's answer has to clear. `now` is the moment the statements are aged
    to. `as_of` is the moment the table is read at, and none means the table as
    it is.
    """
    moment = now or datetime.now(UTC)
    with phase("nearest"):
        rows = await pool.fetch(NEAREST, literal(vector), scope_key, list(seen), model, as_of)
    # The baseline is over every statement, whatever its answer. A short reply
    # scores well against the statements the threshold leaves out, and without
    # them the baseline would fall and let the next statement down through.
    floor = baseline([row["similarity"] for row in rows])
    if floor is None:
        return []
    chosen = [
        dict(row)
        for row in rows
        if not row["seen"] and row["similarity"] > floor + margin and clears(row, actionable)
    ]
    # Similarity says how much the statement is about the prompt, and the
    # weight says how much it is worth reading today, so a fresh correction
    # outranks an old plan that matches the same words.
    for row in chosen:
        row["rank"] = row["similarity"] * worth(row, moment)
    chosen.sort(key=lambda row: row["rank"], reverse=True)
    return chosen[:limit]
