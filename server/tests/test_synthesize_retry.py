"""A retry of the writer's task finishes what the first attempt left undone.

A message's statements are written once, so a retry writes nothing. When the
first attempt failed after the write, the statements it wrote still have to be
embedded and merged, or the match never hands them out and a rule said twice
stays in the table twice.
"""

from datetime import timedelta

import pytest
from _statements import BASE, CUTOFFS, MODEL, NOW, FakeJudge, held, live
from _writer import FakeModel

from agentic_memory.synthesize import synthesize


class OneVector:
    """A model that embeds every statement at the same vector as the older one."""

    model = MODEL

    def documents(self, texts):
        return [BASE for _ in texts]


@pytest.mark.parametrize(
    ("embedded", "merged", "standing"),
    [
        (False, False, {"Commits are not amended."}),
        (True, False, {"Commits are not amended."}),
        (True, True, {"Commits are never amended.", "Commits are not amended."}),
    ],
)
async def test_a_retry_embeds_and_merges_what_the_first_attempt_wrote(
    store, embedded, merged, standing
):
    await held(
        store, "Commits are never amended.", entry_id="earlier", said_at=NOW - timedelta(hours=1)
    )
    await held(
        store,
        "Commits are not amended.",
        entry_id="m1",
        embedding_model=MODEL if embedded else None,
    )
    await store.execute(
        "UPDATE memories SET actionable = 0.5, merged_at = CASE WHEN $1 THEN now() END"
        " WHERE entry_id = 'm1'",
        merged,
    )
    await synthesize(
        "s1",
        "m1",
        settings=CUTOFFS,
        pool=store,
        client=FakeModel("[]"),
        embedder=OneVector(),
        judge=FakeJudge(),
    )
    assert await live(store) == standing
    assert await store.fetchval("SELECT embedding_model FROM memories WHERE entry_id = 'm1'") == (
        MODEL
    )
