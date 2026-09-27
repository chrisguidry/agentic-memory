"""Undoing what one merge run retired, so a rerun asks about the pairs again.

A merge records the run that retired a statement and the run that compared it.
Undoing a run makes its retired statements live again and clears its
comparison marks, and leaves every other run's retirements alone.
"""

from datetime import timedelta

import asyncpg
import pytest
from _statements import CUTOFFS, MODEL, NOW, FakeJudge, at, held, live

from agentic_memory.backfill import parser, run
from agentic_memory.ledger import calling, record
from agentic_memory.memories import retire
from agentic_memory.merge import merge_message
from agentic_memory.merge_pass import merge_backlog
from agentic_memory.unmerge import Refused, undo


@pytest.fixture
async def merged(store: asyncpg.Pool) -> asyncpg.Pool:
    """Two duplicates the run `trial` merged, and a correction no merge made."""
    await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=3))
    await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW - timedelta(hours=2))
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(), run="trial")
    # The correction is far from the duplicates, so a rerun does not merge it.
    elsewhere = at(0.0)
    old = await held(
        store, "The store is SQLite.", vector=elsewhere, said_at=NOW - timedelta(hours=1)
    )
    new = await held(store, "The store is Postgres.", vector=elsewhere, said_at=NOW)
    await retire(store, replaced=[old], replacement=new)
    return store


async def ids(store, *statements: str) -> list[int]:
    found = await store.fetch(
        "SELECT id FROM memories WHERE statement = ANY($1::text[]) ORDER BY id", list(statements)
    )
    return [row["id"] for row in found]


async def test_a_merge_records_the_run_that_retired_a_statement(merged):
    retired = await merged.fetch(
        "SELECT statement, retired_by_run FROM memories WHERE superseded_by IS NOT NULL"
    )
    assert {row["statement"]: row["retired_by_run"] for row in retired} == {
        "Commits are never amended.": "trial",
        "The store is SQLite.": None,
    }


async def test_undoing_a_run_restores_what_it_retired(merged):
    undone = await undo(merged, run="trial")
    assert undone.restored == await ids(merged, "Commits are never amended.")
    assert await live(merged) == {
        "Commits are never amended.",
        "Commits are not amended.",
        "The store is Postgres.",
    }


async def test_undoing_a_run_clears_its_comparison_marks(merged):
    undone = await undo(merged, run="trial")
    assert undone.reopened == await ids(
        merged, "Commits are never amended.", "Commits are not amended."
    )
    assert not await merged.fetchval(
        "SELECT count(*) FROM memories WHERE merged_at IS NOT NULL OR merged_by_run IS NOT NULL"
    )


async def test_a_rerun_after_an_undo_asks_again(merged):
    await undo(merged, run="trial")
    passed = await merge_backlog(settings=CUTOFFS, pool=merged, client=FakeJudge(), run="again")
    assert [(row.reason, row.across) for row in passed.merged] == [("cutoff", False)]


async def test_undoing_another_run_restores_nothing(merged):
    undone = await undo(merged, run="other")
    assert (undone.restored, undone.reopened) == ([], [])
    assert len(await live(merged)) == 2


async def test_undoing_a_run_leaves_an_exact_duplicate_retired(store):
    await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=2))
    await held(store, "Commits are never amended.", vector=at(0.999), said_at=NOW, entry_id="e2")
    await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(), run="trial")
    undone = await undo(store, run="trial")
    assert undone.restored == []
    assert await store.fetchval("SELECT count(*) FROM memories WHERE superseded_by IS NULL") == 1


async def test_a_dry_run_changes_nothing(merged):
    undone = await undo(merged, run="trial", dry_run=True)
    assert (len(undone.restored), len(undone.reopened)) == (1, 2)
    assert len(await live(merged)) == 2
    assert await merged.fetchval("SELECT count(*) FROM memories WHERE merged_by_run = 'trial'") == 2


async def test_the_command_undoes_a_run(merged, postgres_url):
    arguments = parser().parse_args(["unmerge", "--run=trial", f"--database-url={postgres_url}"])
    restored = await ids(merged, "Commits are never amended.")
    reopened = await ids(merged, "Commits are never amended.", "Commits are not amended.")
    assert await run(arguments) == (
        f"restored 1 statements the run trial retired: {restored[0]}\n"
        f"cleared the comparison mark on 2 statements, so a rerun asks about them again:"
        f" {reopened[0]} {reopened[1]}\n"
    )


async def test_the_command_says_what_a_dry_run_would_do(merged, postgres_url):
    arguments = parser().parse_args(
        ["unmerge", "--run=trial", "--dry-run", f"--database-url={postgres_url}"]
    )
    report = await run(arguments)
    assert report.startswith("would restore 1 statements the run trial retired: ")
    assert "would clear the comparison mark on 2 statements" in report
    assert len(await live(merged)) == 2


# The runs the writer merges under: every write, the `/write` and `/reread`
# routes, and the sweep that retries a failed write.
@pytest.mark.parametrize("name", ["live", "write", "reread", "sweep"])
async def test_the_command_refuses_the_writers_runs(merged, postgres_url, name):
    arguments = parser().parse_args(["unmerge", f"--run={name}", f"--database-url={postgres_url}"])
    await merged.execute(
        "UPDATE memories SET retired_by_run = $1 WHERE retired_by_run = 'trial'", name
    )
    with pytest.raises(SystemExit, match="the writer merges under"):
        await run(arguments)
    assert len(await live(merged)) == 2


@pytest.mark.parametrize(
    "window",
    [
        ["--since=2026-09-21T06:00+00:00"],
        ["--until=2026-09-21T12:00+00:00"],
    ],
    ids=["a start with no end", "an end with no start"],
)
async def test_the_command_refuses_half_a_window(postgres_url, window):
    arguments = parser().parse_args(
        ["unmerge", "--run=trial", *window, f"--database-url={postgres_url}"]
    )
    with pytest.raises(SystemExit):
        await run(arguments)


def test_the_command_refuses_a_moment_with_no_offset():
    with pytest.raises(SystemExit):
        parser().parse_args(["unmerge", "--run=trial", "--since=2026-09-21T06:00"])


@pytest.fixture
async def written(store: asyncpg.Pool) -> asyncpg.Pool:
    """A duplicate the writer retired under the run `reread-0927`, as `/reread?run=` does."""
    await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=2))
    await held(store, "Commits are never amended.", vector=at(0.999), said_at=NOW, entry_id="e2")
    await merge_message(
        store,
        FakeJudge(),
        session_id="s1",
        entry_id="e2",
        model=MODEL,
        settings=CUTOFFS,
        run="reread-0927",
    )
    return store


async def called(store: asyncpg.Pool, task: str, run: str) -> None:
    """One model call the ledger records for a task under a run."""
    with calling(task, session_id="s1", entry_id="e2", run=run):
        await record(store, provider="typesafe", model="m", duration_ms=1, outcome="ok")


# A run whose ledger holds a call the writer makes is a run the writer merged
# under, whatever it is named, because `/write` and `/reread` take any name.
@pytest.mark.parametrize("task", ["synthesize", "classify"])
async def test_undo_refuses_a_run_the_writer_merged_under(written, task):
    await called(written, task, "reread-0927")
    with pytest.raises(Refused, match="the writer merges under"):
        await undo(written, run="reread-0927")
    assert await written.fetchval("SELECT count(*) FROM memories WHERE superseded_by IS NULL") == 1


@pytest.mark.parametrize(
    ("task", "run_of_call"),
    [("merge", "reread-0927"), ("synthesize", "live")],
    ids=["a merge call", "a writer call under another run"],
)
async def test_undo_accepts_a_run_with_no_writer_call(written, task, run_of_call):
    await called(written, task, run_of_call)
    undone = await undo(written, run="reread-0927")
    assert undone.reopened != []
