"""The statements that are about a prompt.

The prompt is embedded and compared with every live statement reachable from
the scope. The few that score well above the rest are handed over, and most
prompts get nothing, because most prompts are about the work in front of the
person and the record has nothing to add.
"""

import asyncio
import math
from datetime import UTC, datetime

import asyncpg

from .embed import Embedder, literal
from .memories import worth

# Every reachable live statement this model has embedded, nearest first, with
# whether the session was already handed it. The seen ones are read too, and
# not filtered here, because the baseline below is over the whole scope and
# must not move as a session is handed more.
NEAREST = """
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at, actor, actor_depth,
           1 - (embedding <=> $1::vector) AS similarity,
           EXISTS (
               SELECT 1 FROM injections i
               WHERE i.session_id = $3 AND memories.id = ANY(i.memory_ids)
           ) AS seen
    FROM memories
    WHERE superseded_by IS NULL
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


async def match(
    pool: asyncpg.Pool,
    embedder: Embedder,
    *,
    session_id: str,
    scope_key: str | None,
    prompt: str,
    limit: int,
    margin: float,
    now: datetime | None = None,
) -> list[dict]:
    """The statements about this prompt that the session has not seen, best first."""
    moment = now or datetime.now(UTC)
    vector = await asyncio.to_thread(embedder.query, prompt)
    rows = await pool.fetch(NEAREST, literal(vector), scope_key, session_id, embedder.model)
    floor = baseline([row["similarity"] for row in rows])
    if floor is None:
        return []
    chosen = [dict(row) for row in rows if not row["seen"] and row["similarity"] > floor + margin]
    # Similarity says how much the statement is about the prompt, and the
    # weight says how much it is worth reading today, so a fresh correction
    # outranks an old plan that matches the same words.
    for row in chosen:
        row["rank"] = row["similarity"] * worth(row, moment)
    chosen.sort(key=lambda row: row["rank"], reverse=True)
    return chosen[:limit]
