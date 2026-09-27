"""The third question: whether the newer statement keeps what the older one said.

The first question says yes to two statements that are only about one area.
It retires a general rule into one case of it, and a statement into a newer one
that leaves out part of it, and the retired part is then in no live statement.
A pair that says the same thing merges only when the model also says the newer
statement keeps everything a reader needs from the older one. A newer statement
that settles the older one in a different way changes it on purpose, so that
merge does not ask for this.
"""

from datetime import timedelta

import pytest
from _statements import CUTOFFS, MODEL, NOW, FakeJudge, at, held, live

from agentic_memory.merge import merge

# Pairs where the newer statement is about the same area and loses part of the
# older one: a rule it leaves out, a different fact, or a general rule narrowed
# to one case. None holds a literal the other lacks, so only the model's answers
# keep them apart. The fake judge answers the same whatever the text, so these
# sentences show the kind of pair each case stands for, and do not test the model.
LOSSES = [
    (
        "Tests come before the code, table-driven, and faking only at the kernel boundary.",
        "Tests come first and are table-driven.",
    ),
    (
        "The cache holds each page for an hour and is cleared on deploy.",
        "The cache replaces the old in-memory map.",
    ),
    (
        "Always run the whole suite, not only the touched tests.",
        "Run the tests before a commit.",
    ),
]

# Pairs where the newer statement says the older one in other words, and may add
# a detail.
PARAPHRASES = [
    ("Commits are never amended.", "Commit history is never rewritten with an amend."),
    ("Use uv for Python work, not pip.", "Python projects are managed with uv rather than pip."),
    (
        "The widget service stores its state in Postgres.",
        "Postgres holds the widget service's state, with an index on each owner.",
    ),
]


async def pair(store, older: str, newer: str) -> tuple[int, int]:
    """The two statements in the band, the older one said a day before."""
    first = await held(store, older, said_at=NOW - timedelta(days=1))
    second = await held(store, newer, vector=at(0.90), said_at=NOW)
    return first, second


@pytest.mark.parametrize(("older", "newer"), LOSSES)
async def test_a_newer_statement_that_loses_part_of_the_older_leaves_both(store, older, newer):
    ids = await pair(store, older, newer)
    judge = FakeJudge(same=True, keeps=False)
    await merge(store, judge, statement_id=ids[1], model=MODEL, settings=CUTOFFS)
    assert await live(store) == {older, newer}


@pytest.mark.parametrize(("older", "newer"), PARAPHRASES)
async def test_a_paraphrase_that_keeps_the_older_statement_retires_it(store, older, newer):
    ids = await pair(store, older, newer)
    judge = FakeJudge(same=True, keeps=True)
    (merged,) = await merge(store, judge, statement_id=ids[1], model=MODEL, settings=CUTOFFS)
    assert (merged.retired, merged.survivor, merged.reason) == (*ids, "same")
    assert await live(store) == {newer}


@pytest.mark.parametrize(("older", "newer"), LOSSES)
async def test_a_loss_is_not_retired_by_the_second_question_instead(store, older, newer):
    # A yes to the first question means the newer one did not settle the older
    # one differently, so a yes to the second is the loss said another way.
    ids = await pair(store, older, newer)
    judge = FakeJudge(same=True, keeps=False, settles=True)
    await merge(store, judge, statement_id=ids[1], model=MODEL, settings=CUTOFFS)
    assert await live(store) == {older, newer}


async def test_a_changed_decision_retires_without_keeping_the_older(store):
    ids = await pair(store, "The widget's store is SQLite.", "The widget's store is Postgres.")
    judge = FakeJudge(same=False, settles=True, keeps=False)
    (merged,) = await merge(store, judge, statement_id=ids[1], model=MODEL, settings=CUTOFFS)
    assert (merged.retired, merged.reason) == (ids[0], "settles")


@pytest.mark.parametrize(("threshold", "standing"), [(0.4, 1), (0.45, 1), (0.5, 2)])
async def test_the_third_question_has_its_own_threshold(store, threshold, standing):
    ids = await pair(store, *PARAPHRASES[0])
    settings = CUTOFFS.model_copy(update={"merge_keeps": threshold})
    judge = FakeJudge(same=True, keeps=0.45)
    await merge(store, judge, statement_id=ids[1], model=MODEL, settings=settings)
    assert len(await live(store)) == standing


async def test_a_merge_on_the_cutoff_asks_nothing(store):
    older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
    newer = await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
    judge = FakeJudge(keeps=False)
    (merged,) = await merge(store, judge, statement_id=newer, model=MODEL, settings=CUTOFFS)
    assert (merged.retired, merged.reason) == (older, "cutoff")
    assert judge.asked == []
