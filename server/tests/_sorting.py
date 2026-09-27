"""What the sort's tests share: a model, a prospective statement, and reading it back.

The sort is tested on its own and on the writer's path, and both need a model
that answers the sort's questions and a statement in the store to sort.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
from typesafe_sdk import Choice

SCOPE = "example.test/acme/widget"

# Late on a Friday evening in New York, which is already Saturday in UTC.
FRIDAY_EVENING = datetime(2026, 9, 26, 1, 44, tzinfo=UTC)


class FakeJudge:
    """A System One model that gives each question the answer it was set up with.

    A yes/no question is answered with a probability, and a choice with the
    label named for it. A question with no answer set is answered no, or with
    the first label.
    """

    def __init__(self, **answers):
        self.answers = answers
        self.asked: list[dict] = []

    async def system_one(self, *, state, questions):
        self.asked.append(state)
        nouls, choices = {}, {}
        for name, question in questions.items():
            if isinstance(question, Choice):
                label = self.answers.get(name, next(iter(question.criteria)))
                choices[name] = SimpleNamespace(
                    choice=label, confidence=0.9, probabilities={label: 0.9}
                )
            else:
                nouls[name] = SimpleNamespace(noul=self.answers.get(name, 0.1))
        return SimpleNamespace(model="jev-1.13.0", nouls=nouls, choices=choices)


async def held(
    store: asyncpg.Pool,
    statement: str = "Do not release the widget before Monday.",
    *,
    kind: str = "prospective",
    entry_id: str = "e1",
    said_at: datetime = FRIDAY_EVENING,
) -> int:
    """One live statement in the store, and its id."""
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, actor, actor_depth, actionable)
        VALUES ($1, $2, 0.9, $3, 's1', $4, 'jev-1.13.0', 'fp', $5, 'person', 0, 0.6)
        RETURNING id
        """,
        statement,
        kind,
        SCOPE,
        entry_id,
        said_at,
    )


async def row_of(store: asyncpg.Pool, statement_id: int) -> asyncpg.Record:
    return await store.fetchrow("SELECT * FROM memories WHERE id = $1", statement_id)
