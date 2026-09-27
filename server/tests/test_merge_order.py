"""What a merge retires a statement into, and when it marks a statement compared.

A statement retires only into a live statement it was compared with, so a
chain of pointers never skips a comparison the model did not make. A statement
is marked compared only once its retirements are stored, so a failure between
the two is finished by a retry.
"""

from datetime import timedelta

import pytest
from _statements import CUTOFFS, MODEL, NOW, FakeJudge, at, held, live

import agentic_memory.merge as merge_module
from agentic_memory.memories import end
from agentic_memory.merge import merge, merge_message


def pointer(store, statement_id: int):
    return store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", statement_id)


async def test_a_failure_between_the_answers_and_the_retirement_is_retried(store, monkeypatch):
    await held(
        store, "Commits are never amended.", entry_id="old", said_at=NOW - timedelta(hours=1)
    )
    await held(store, "Commits are not amended.", entry_id="m1", vector=at(0.99), said_at=NOW)

    async def dropped(*args, **kwargs):
        raise ConnectionError("the connection dropped")

    with monkeypatch.context() as patched:
        patched.setattr(merge_module, "retire", dropped)
        with pytest.raises(ConnectionError):
            await merge_message(
                store, FakeJudge(), session_id="s1", entry_id="m1", model=MODEL, settings=CUTOFFS
            )
    await merge_message(
        store, FakeJudge(), session_id="s1", entry_id="m1", model=MODEL, settings=CUTOFFS
    )
    assert await live(store) == {"Commits are not amended."}


async def test_every_older_neighbour_points_straight_at_the_statement(store):
    # The write merges the newest statement, so the ones it retires all name it,
    # and the count of rows that point at it is the count of duplicates.
    oldest = await held(store, "A", said_at=NOW - timedelta(hours=3))
    middle = await held(store, "B", vector=at(0.99), said_at=NOW - timedelta(hours=2))
    newest = await held(store, "C", vector=at(0.98), said_at=NOW - timedelta(hours=1))
    await merge(store, FakeJudge(), statement_id=newest, model=MODEL, settings=CUTOFFS)
    assert await live(store) == {"C"}
    assert [await pointer(store, oldest), await pointer(store, middle)] == [newest, newest]


@pytest.fixture
async def two_newer(store) -> tuple[int, int, int]:
    """A statement written late, and two newer neighbours that each settle it again."""
    late = await held(store, "Lint with ruff.", entry_id="s", said_at=NOW - timedelta(hours=3))
    first = await held(
        store, "Lint with flake8.", entry_id="a", vector=at(0.90), said_at=NOW - timedelta(hours=2)
    )
    second = await held(
        store,
        "Lint only changed files.",
        entry_id="b",
        vector=at(0.85),
        said_at=NOW - timedelta(hours=1),
    )
    return late, first, second


async def test_a_neighbour_retires_only_into_a_statement_it_was_compared_with(store, two_newer):
    late, first, second = two_newer
    judge = FakeJudge(same=False, settles=True)
    await merge(store, judge, statement_id=late, model=MODEL, settings=CUTOFFS)
    assert await live(store) == {"Lint with flake8.", "Lint only changed files."}
    assert await pointer(store, late) == second
    assert await pointer(store, first) is None


class EndsTheSurvivor(FakeJudge):
    """A model that is asked while a message ends the statement that would survive."""

    def __init__(self, store, survivor: int):
        super().__init__(same=True)
        self.store = store
        self.survivor = survivor

    async def system_one(self, *, state, questions):
        await end(self.store, ended=[self.survivor], reason="event")
        return await super().system_one(state=state, questions=questions)


async def test_nothing_retires_into_a_statement_that_is_no_longer_live(store):
    older = await held(
        store, "Commits are never amended.", entry_id="old", said_at=NOW - timedelta(hours=1)
    )
    newer = await held(
        store, "Commit history is never rewritten.", entry_id="new", vector=at(0.90), said_at=NOW
    )
    judge = EndsTheSurvivor(store, newer)
    await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
    assert await live(store) == {"Commits are never amended."}
