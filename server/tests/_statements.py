"""What the merge's tests share: statements with chosen vectors, and a judge.

The statements carry hand-built unit vectors, so a similarity is exactly the
number the test chose, and the store is still the real one pgvector runs the
query in.
"""

import math
from datetime import UTC, datetime
from types import SimpleNamespace

from agentic_memory.embed import literal
from agentic_memory.memories import memories
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "github.com/acme/widget"
MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# A unit vector at an angle from the first axis. Two of them have the cosine of
# the angle between them, so `at(s)` is at similarity `s` from `BASE` exactly.
BASE = [1.0] + [0.0] * (DIMENSIONS - 1)


def at(similarity: float) -> list[float]:
    angle = math.acos(similarity)
    return [math.cos(angle), math.sin(angle)] + [0.0] * (DIMENSIONS - 2)


# The merge's settings as they ship, and the same with merging across kinds on.
CUTOFFS = Settings(embed_model=MODEL, merge_upper=0.95, merge_lower=0.80)
ACROSS = CUTOFFS.model_copy(update={"merge_across_kinds": True})


def probability(answer: bool | float) -> float:
    """A yes or a no as a clear probability, or the probability the test chose."""
    if isinstance(answer, float):
        return answer
    return 0.9 if answer else 0.1


class FakeJudge:
    """A System One model that gives the same answers about every pair it is asked."""

    def __init__(
        self,
        same: bool | float = True,
        settles: bool | float = False,
        keeps: bool | float = True,
    ):
        self.answers = {
            "same": probability(same),
            "settles": probability(settles),
            "keeps": probability(keeps),
        }
        self.asked: list[tuple[str, str]] = []

    async def system_one(self, *, state, questions):
        self.asked.append((state["first"], state["second"]))
        return SimpleNamespace(
            nouls={name: SimpleNamespace(noul=self.answers[name]) for name in questions}
        )


async def held(
    store,
    statement: str,
    *,
    kind: str = "preference",
    scope_key: str | None = SCOPE,
    vector: list[float] | None = None,
    embedding_model: str | None = MODEL,
    said_at: datetime | None = NOW,
    session_id: str = "s1",
    entry_id: str | None = None,
) -> int:
    """One live statement with a vector the test chose, or with none."""
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, embedding, embedding_model)
        VALUES ($1, $2, 0.9, $3, $4, $5, 'jev-1.13.0', 'fp', $6, $7::vector, $8)
        RETURNING id
        """,
        statement,
        kind,
        scope_key,
        session_id,
        entry_id or statement,
        said_at,
        literal(vector or BASE) if embedding_model else None,
        embedding_model,
    )


async def live(store, scope_key: str | None = SCOPE) -> set[str]:
    return {row["statement"] for row in await memories(store, scope_key=scope_key, now=NOW)}
