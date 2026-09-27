"""The backfill command, against a real store.

The commands that answer the statements and merge them call a model over the
network, so only their pricing runs here. The command that embeds the readings
runs whole, because its model is local.
"""

import json
from datetime import timedelta

import asyncpg
import pytest
from _statements import NOW, SCOPE, at, held

from agentic_memory.backfill import parser, run
from agentic_memory.classify import KIND_COLUMNS


@pytest.fixture
async def readings(store: asyncpg.Pool) -> asyncpg.Pool:
    scores = dict.fromkeys(KIND_COLUMNS, 0.05)
    for entry_id, message in [("e1", "keep going"), ("e2", "looks good"), ("e3", "ship it")]:
        await store.execute(
            f"""
            INSERT INTO classifications
                (session_id, entry_id, model, questions_fingerprint, rounds, state,
                 {", ".join(scores)})
            VALUES ('s1', $1, 'jev-1.13.0', 'fp', 5, $2::jsonb,
                    {", ".join(f"${number}" for number in range(3, 3 + len(scores)))})
            """,
            entry_id,
            json.dumps({"before": "", "message": message}),
            *scores.values(),
        )
    return store


@pytest.mark.parametrize("batch", [[], ["--batch=1"], ["--batch=2"]])
async def test_the_readings_are_embedded(
    readings: asyncpg.Pool, postgres_url: str, embedder, batch: list[str]
):
    report = await run(parser().parse_args(["readings", *batch, f"--database-url={postgres_url}"]))
    assert report.startswith("embedded 3 readings")
    assert not await readings.fetchval(
        "SELECT count(*) FROM classifications WHERE prompt_embedding_model IS NULL"
    )


@pytest.fixture
async def unanswered(store: asyncpg.Pool) -> asyncpg.Pool:
    for number in range(3):
        await store.execute(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model, questions_fingerprint)
            VALUES ($1, 'semantic', 0.9, 's1', $1, 'jev-1.13.0', 'fp')
            """,
            f"Rule {number} of the widget.",
        )
    return store


async def test_the_price_names_what_is_left_before_any_call(unanswered, postgres_url):
    arguments = parser().parse_args(
        ["actionable", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == "3 statements unanswered, and no calls to price them from\n"


async def test_the_price_is_the_calls_already_made_times_what_is_left(unanswered, postgres_url):
    await unanswered.execute(
        """
        INSERT INTO model_calls (provider, model, task, input_tokens, output_tokens, outcome)
        VALUES ('typesafe', 'jev-1.13.0', 'actionable', 400, 20, 'ok')
        """
    )
    arguments = parser().parse_args(
        ["actionable", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == (
        "3 statements unanswered, about 1200 input and 60 output tokens\n"
    )


@pytest.fixture
async def unsorted(store: asyncpg.Pool) -> asyncpg.Pool:
    for number in range(4):
        await store.execute(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model, questions_fingerprint)
            VALUES ($1, 'prospective', 0.9, 's1', $1, 'jev-1.13.0', 'fp')
            """,
            f"Do not release widget {number} before Monday.",
        )
    return store


@pytest.mark.parametrize(
    "limit, expected",
    [([], "4 prospective statements unsorted"), (["--limit=3"], "3 prospective statements")],
)
async def test_the_sort_price_names_what_is_left_before_any_call(
    unsorted, postgres_url, limit, expected
):
    arguments = parser().parse_args(
        ["sort", "--run=first", "--price", *limit, f"--database-url={postgres_url}"]
    )
    assert (await run(arguments)).startswith(expected)


async def test_the_sort_price_is_the_calls_already_made_times_what_is_left(unsorted, postgres_url):
    await unsorted.execute(
        """
        INSERT INTO model_calls (provider, model, task, input_tokens, output_tokens, outcome)
        VALUES ('typesafe', 'jev-1.13.0', 'sort', 2000, 40, 'ok'),
               ('typesafe', 'jev-1.13.0', 'actionable', 400, 20, 'ok')
        """
    )
    arguments = parser().parse_args(
        ["sort", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == (
        "4 prospective statements unsorted, about 8000 input and 160 output tokens\n"
    )


@pytest.fixture
async def unmerged(store: asyncpg.Pool) -> asyncpg.Pool:
    """Four statements in one scope, three pairs of them in the band, and one elsewhere.

    The first is in the band with each of the other three, and those three are
    above the upper cutoff with each other, so they are not asked about.
    """
    await held(store, "A", said_at=NOW - timedelta(hours=4))
    await held(store, "B", vector=at(0.90), said_at=NOW - timedelta(hours=3))
    await held(store, "C", vector=at(0.86), said_at=NOW - timedelta(hours=2))
    await held(store, "D", vector=at(0.82), said_at=NOW - timedelta(hours=1))
    await held(store, "E", scope_key="github.com/acme/other", said_at=NOW)
    return store


async def test_the_merge_price_names_the_pairs_before_any_call(unmerged, postgres_url):
    arguments = parser().parse_args(
        ["merge", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == (
        "5 statements and at most 3 pairs to ask,"
        " and no calls under the run first to price them from\n"
    )


@pytest.mark.parametrize(
    ("calls", "priced"),
    [
        ([("live", 400, 20)], "and no calls under the run first to price them from"),
        ([("live", 400, 20), ("first", 600, 30)], "about 1800 input and 90 output tokens"),
    ],
)
async def test_the_merge_price_reads_only_the_calls_of_its_own_run(
    unmerged, postgres_url, calls, priced
):
    # A merge call asked one question before the second was added, so a merge
    # call from another run can be a smaller call than the one being priced.
    await unmerged.executemany(
        """
        INSERT INTO model_calls (provider, model, task, run, input_tokens, output_tokens, outcome)
        VALUES ('typesafe', 'jev-1.13.0', 'merge', $1, $2, $3, 'ok')
        """,
        calls,
    )
    arguments = parser().parse_args(
        ["merge", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == f"5 statements and at most 3 pairs to ask, {priced}\n"


@pytest.mark.parametrize(
    ("again", "priced"),
    [([], "0 statements and at most 0 pairs"), (["--again"], "5 statements and at most 3 pairs")],
)
async def test_the_merge_price_leaves_out_what_was_compared_unless_asked(
    unmerged, postgres_url, again, priced
):
    await unmerged.execute("UPDATE memories SET merged_at = now()")
    arguments = parser().parse_args(
        ["merge", "--run=first", "--price", *again, f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == (
        f"{priced} to ask, and no calls under the run first to price them from\n"
    )


async def test_the_merge_price_counts_the_oldest_statements_of_one_scope(unmerged, postgres_url):
    arguments = parser().parse_args(
        [
            "merge",
            "--run=first",
            "--price",
            f"--scope={SCOPE}",
            "--limit=3",
            f"--database-url={postgres_url}",
        ]
    )
    assert await run(arguments) == (
        "3 statements and at most 2 pairs to ask,"
        " and no calls under the run first to price them from\n"
    )


async def test_the_merge_price_counts_only_pairs_of_one_kind_by_default(store, postgres_url):
    await held(store, "A", kind="praise", said_at=NOW - timedelta(hours=1))
    await held(store, "B", kind="semantic", vector=at(0.90), said_at=NOW)
    arguments = parser().parse_args(
        ["merge", "--run=first", "--price", f"--database-url={postgres_url}"]
    )
    assert await run(arguments) == (
        "2 statements and at most 0 pairs to ask,"
        " and no calls under the run first to price them from\n"
    )
