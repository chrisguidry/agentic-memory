"""The pass that merges every live statement already in the table.

It takes the statements oldest first, and each one meets the live statements
said before it, so each pair is asked about once and the survivor of a group is
its newest statement.
"""

import inspect
from datetime import timedelta

import pytest
from _statements import ACROSS, CUTOFFS, MODEL, NOW, SCOPE, FakeJudge, at, held, live

from agentic_memory.app import merge_all
from agentic_memory.embed import embed_rows
from agentic_memory.merge_pass import merge_backlog, merge_statements

OTHER = "github.com/acme/other"


async def test_the_pass_leaves_no_pair_above_the_upper_cutoff(store):
    await held(store, "A", said_at=NOW - timedelta(hours=3))
    await held(store, "B", vector=at(0.99), said_at=NOW - timedelta(hours=2))
    await held(store, "C", vector=at(0.98), said_at=NOW - timedelta(hours=1))
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(same=False))
    assert await live(store) == {"C"}


async def test_the_pass_leaves_a_band_pair_the_model_rejected(store):
    await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
    await held(store, "Commits are always amended.", vector=at(0.90), said_at=NOW)
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(same=False))
    assert len(await live(store)) == 2


async def test_the_pass_asks_about_each_pair_once(store):
    await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
    await held(store, "Commits are always amended.", vector=at(0.90), said_at=NOW)
    judge = FakeJudge(same=False)
    await merge_backlog(settings=CUTOFFS, pool=store, client=judge)
    assert judge.asked == [("Commits are never amended.", "Commits are always amended.")]


async def test_only_the_model_being_compared_is_a_candidate(store):
    await held(store, "A", embedding_model="another-model", said_at=NOW - timedelta(hours=1))
    await held(store, "B", vector=at(0.99), embedding_model=MODEL, said_at=NOW)
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge())
    assert len(await live(store)) == 2


@pytest.fixture
async def two_kinds(store):
    """Praise and a fact in the band, which the model says are the same thing."""
    await held(store, "A", kind="praise", said_at=NOW - timedelta(hours=1))
    await held(store, "B", kind="semantic", vector=at(0.90), said_at=NOW)
    return store


async def test_the_pass_keeps_kinds_apart_by_default(two_kinds):
    judge = FakeJudge(same=True)
    passed = await merge_backlog(settings=CUTOFFS, pool=two_kinds, client=judge)
    assert passed.merged == []
    assert judge.asked == []


async def test_the_pass_says_what_it_merged_across_kinds(two_kinds):
    passed = await merge_backlog(settings=ACROSS, pool=two_kinds, client=FakeJudge(same=True))
    assert [(row.reason, row.across) for row in passed.merged] == [("same", True)]


@pytest.fixture
async def two_scopes(store):
    """A pair of duplicates in each of two scopes, the older pair in the other scope."""
    await held(store, "A", scope_key=OTHER, said_at=NOW - timedelta(hours=4))
    await held(store, "B", scope_key=OTHER, vector=at(0.99), said_at=NOW - timedelta(hours=3))
    await held(store, "C", said_at=NOW - timedelta(hours=2))
    await held(store, "D", vector=at(0.99), said_at=NOW - timedelta(hours=1))
    return store


class FailsInOther(FakeJudge):
    """A model that times out on every pair in the other scope."""

    async def system_one(self, *, state, questions):
        if state["first"].startswith("other"):
            raise TimeoutError("the provider timed out")
        return await super().system_one(state=state, questions=questions)


@pytest.fixture
async def band_pairs(store) -> dict[str, int]:
    """A pair in the band in each of two scopes."""
    return {
        name: await held(store, name, scope_key=scope, vector=vector, said_at=NOW - age)
        for name, scope, vector, age in [
            ("other A", OTHER, None, timedelta(hours=2)),
            ("other B", OTHER, at(0.90), timedelta(hours=1)),
            ("mine A", SCOPE, None, timedelta(hours=2)),
            ("mine B", SCOPE, at(0.90), timedelta(hours=1)),
        ]
    }


async def test_a_failure_in_one_scope_does_not_stop_the_others(store, band_pairs):
    passed = await merge_backlog(settings=CUTOFFS, pool=store, client=FailsInOther(), concurrency=2)
    assert [row.retired for row in passed.merged] == [band_pairs["mine A"]]
    assert passed.failed == [band_pairs["other B"]]


async def test_a_rerun_asks_only_about_what_failed(store, band_pairs):
    await merge_backlog(settings=CUTOFFS, pool=store, client=FailsInOther())
    judge = FakeJudge()
    await merge_backlog(settings=CUTOFFS, pool=store, client=judge)
    assert judge.asked == [("other A", "other B")]


@pytest.mark.parametrize(("again", "asked"), [(False, 0), (True, 2)])
async def test_a_statement_already_compared_is_compared_again_only_when_asked(
    store, band_pairs, again, asked
):
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(same=False))
    judge = FakeJudge(same=False)
    await merge_backlog(settings=CUTOFFS, pool=store, client=judge, again=again)
    assert len(judge.asked) == asked


class Moved:
    """A model that embeds every statement at a new vector, still in the band."""

    model = MODEL

    def documents(self, texts):
        return [at(0.88) for _ in texts]


async def test_a_statement_embedded_again_is_compared_again(store, band_pairs):
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(same=False))
    rows = await store.fetch(
        "SELECT id, statement FROM memories WHERE id = $1", band_pairs["mine B"]
    )
    await embed_rows(store, Moved(), rows)
    judge = FakeJudge(same=False)
    await merge_backlog(settings=CUTOFFS, pool=store, client=judge)
    assert judge.asked == [("mine A", "mine B")]


def test_the_task_and_the_route_name_the_run_alike():
    task = inspect.signature(merge_statements).parameters["run"].default
    route = inspect.signature(merge_all).parameters["run"].default
    assert task == route


@pytest.mark.parametrize("concurrency", [1, 4])
async def test_the_answer_is_the_same_at_any_concurrency(two_scopes, concurrency):
    await merge_backlog(
        settings=CUTOFFS, pool=two_scopes, client=FakeJudge(), concurrency=concurrency
    )
    assert await live(two_scopes, scope_key=None) == {"B", "D"}


@pytest.mark.parametrize(
    ("scope", "limit", "standing"),
    [
        (SCOPE, None, {"A", "B", "D"}),
        (OTHER, None, {"B", "C", "D"}),
        (None, 2, {"B", "C", "D"}),
        (None, 3, {"B", "C", "D"}),
        (SCOPE, 1, {"A", "B", "C", "D"}),
    ],
)
async def test_the_pass_takes_one_scope_and_the_oldest_statements(
    two_scopes, scope, limit, standing
):
    await merge_backlog(
        settings=CUTOFFS, pool=two_scopes, client=FakeJudge(), scope=scope, limit=limit
    )
    assert await live(two_scopes, scope_key=None) == standing
