"""The two questions the model answers about a pair in the band.

The first asks whether the two say the same thing. The second asks whether the
newer one settles the question the older one settled, in a different way, which
is how a changed decision retires the one it changed rather than standing beside
it.
"""

from datetime import timedelta

import pytest
from _statements import ACROSS, CUTOFFS, MODEL, NOW, FakeJudge, at, held, live
from typesafe_sdk import TypeSafeBadRequestError

from agentic_memory.merge import merge

OLDER = "The widget's store is SQLite."
NEWER = "The widget's store is Postgres."


@pytest.fixture
async def changed(store) -> tuple[int, int]:
    """An older decision and a newer one that changed it, in the band."""
    older = await held(store, OLDER, said_at=NOW - timedelta(days=3))
    newer = await held(store, NEWER, vector=at(0.90), said_at=NOW)
    return older, newer


async def test_a_newer_statement_that_changes_an_older_one_retires_it(store, changed):
    older, newer = changed
    judge = FakeJudge(same=False, settles=True)
    await merge(store, judge, statement_id=newer, model=MODEL, settings=CUTOFFS)
    assert await live(store) == {NEWER}
    assert await store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", older) == newer


@pytest.mark.parametrize("subject", [0, 1])
async def test_the_older_statement_is_always_first_in_the_question(store, changed, subject):
    judge = FakeJudge(same=False)
    await merge(store, judge, statement_id=changed[subject], model=MODEL, settings=CUTOFFS)
    assert judge.asked == [(OLDER, NEWER)]


@pytest.mark.parametrize(
    ("same", "settles", "reason"),
    [
        (True, False, "same"),
        (False, True, "settles"),
        (True, True, "same"),
    ],
)
async def test_a_merge_says_which_question_retired_the_older(store, changed, same, settles, reason):
    older, newer = changed
    judge = FakeJudge(same=same, settles=settles)
    (merged,) = await merge(store, judge, statement_id=newer, model=MODEL, settings=CUTOFFS)
    assert (merged.retired, merged.survivor, merged.reason) == (older, newer, reason)
    assert not merged.across


async def test_a_merge_on_the_cutoff_says_so(store):
    older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
    newer = await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
    (merged,) = await merge(store, FakeJudge(), statement_id=newer, model=MODEL, settings=CUTOFFS)
    assert (merged.retired, merged.reason, merged.across) == (older, "cutoff", False)


@pytest.mark.parametrize(("threshold", "standing"), [(0.5, 1), (0.95, 2)])
async def test_the_second_question_has_its_own_threshold(store, changed, threshold, standing):
    settings = CUTOFFS.model_copy(update={"merge_settles": threshold})
    judge = FakeJudge(same=False, settles=True)
    await merge(store, judge, statement_id=changed[1], model=MODEL, settings=settings)
    assert len(await live(store)) == standing


@pytest.mark.parametrize(("settles", "standing"), [(0.65, 2), (0.75, 1)])
async def test_a_hesitant_second_answer_retires_nothing_by_default(
    store, changed, settles, standing
):
    judge = FakeJudge(same=False, settles=settles)
    await merge(store, judge, statement_id=changed[1], model=MODEL, settings=CUTOFFS)
    assert len(await live(store)) == standing


async def test_a_merge_across_kinds_says_so(store):
    older = await held(store, OLDER, kind="praise", said_at=NOW - timedelta(days=3))
    newer = await held(store, NEWER, kind="semantic", vector=at(0.90), said_at=NOW)
    judge = FakeJudge(same=False, settles=True)
    (merged,) = await merge(store, judge, statement_id=newer, model=MODEL, settings=ACROSS)
    assert (merged.retired, merged.reason, merged.across) == (older, "settles", True)


async def test_two_statements_from_one_moment_are_not_settled_by_each_other(store):
    # Neither was said after the other, so neither can have changed the other.
    first = await held(store, OLDER)
    await held(store, NEWER, vector=at(0.90))
    judge = FakeJudge(same=False, settles=True)
    await merge(store, judge, statement_id=first, model=MODEL, settings=CUTOFFS)
    assert len(await live(store)) == 2


class Refusing:
    """A System One model that refuses every request."""

    async def system_one(self, *, state, questions):
        raise TypeSafeBadRequestError(400, {"detail": {"error_type": "max_tokens_exceeded"}}, {})


async def test_a_refused_pair_both_stand(store, changed):
    assert (
        await merge(store, Refusing(), statement_id=changed[1], model=MODEL, settings=CUTOFFS) == []
    )
    assert len(await live(store)) == 2
