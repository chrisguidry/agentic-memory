"""Undoing the merges made before the merge recorded its run.

Such a retirement has no run on its row, so a window of time finds it: every
retirement with no run in the window whose survivor's text differs from its
own. A correction and the sort record no run either, so a window restores them
too, and a dry run shows what a window would restore before anything changes.
"""

from datetime import timedelta

import pytest
from _statements import NOW, held

from agentic_memory.unmerge import Refused, undo

WINDOW = {"since": NOW - timedelta(hours=6), "until": NOW}


async def unrecorded(store, statement: str, survivor: str, retired_at, merged_at=None) -> int:
    """A statement retired into another before the merge recorded its run."""
    kept = await held(store, survivor, entry_id=f"{statement} survivor")
    gone = await held(store, statement)
    await store.execute(
        "UPDATE memories SET superseded_by = $2, superseded_at = $3, merged_at = $4 WHERE id = $1",
        gone,
        kept,
        retired_at,
        merged_at,
    )
    return gone


@pytest.mark.parametrize(
    ("statement", "survivor", "retired_at", "restored"),
    [
        ("The API is at https://api.example.test/v1.", "Use the API.", NOW - timedelta(hours=1), 1),
        ("Use the API.", "Use the API.", NOW - timedelta(hours=1), 0),
        ("The API is at https://api.example.test/v1.", "Use the API.", NOW - timedelta(days=2), 0),
        ("The API is at https://api.example.test/v1.", "Use the API.", NOW + timedelta(hours=1), 0),
    ],
    ids=["in the window", "same text", "before the window", "after the window"],
)
async def test_a_window_restores_the_unrecorded_retirements_in_it(
    store, statement, survivor, retired_at, restored
):
    await unrecorded(store, statement, survivor, retired_at)
    undone = await undo(store, run="trial", **WINDOW)
    assert len(undone.restored) == restored


@pytest.mark.parametrize(
    ("merged_at", "reopened"),
    [(NOW - timedelta(hours=1), 1), (NOW - timedelta(days=2), 0), (None, 0)],
    ids=["in the window", "before the window", "never compared"],
)
async def test_a_window_clears_the_unrecorded_comparison_marks_in_it(store, merged_at, reopened):
    await unrecorded(store, "Widgets ship on Fridays.", "Ship.", NOW - timedelta(days=3), merged_at)
    undone = await undo(store, run="trial", **WINDOW)
    assert len(undone.reopened) == reopened


async def test_a_dry_run_names_what_a_window_would_restore(store):
    gone = await unrecorded(
        store,
        "Widgets ship on Fridays.",
        "Ship.",
        NOW - timedelta(hours=1),
        NOW - timedelta(hours=1),
    )
    undone = await undo(store, run="trial", **WINDOW, dry_run=True)
    assert (undone.restored, undone.reopened) == ([gone], [gone])
    assert await store.fetchval(
        "SELECT superseded_by IS NOT NULL FROM memories WHERE id = $1", gone
    )


@pytest.mark.parametrize(
    "window",
    [
        {"since": NOW - timedelta(hours=6)},
        {"until": NOW},
        {"since": NOW, "until": NOW - timedelta(hours=6)},
    ],
    ids=["a start with no end", "an end with no start", "a start after the end"],
)
async def test_undo_refuses_a_window_that_is_not_one(store, window):
    with pytest.raises(Refused, match="window"):
        await undo(store, run="trial", **window)
