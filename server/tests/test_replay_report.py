"""The report on a replay, and the pairs it hands out.

The counts are computed from turns built here, because what the report proves
is its arithmetic. The labels are read from the store, because the labels
table is shared with the labelling side and the read has to match its shape.
Every prompt and statement here is invented.
"""

from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from agentic_memory.recall import Handout
from agentic_memory.replay.report import Pair, labels, pairs, render, summarize, write_pairs
from agentic_memory.replay.run import Prompt, Turn

MONDAY = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def statement(memory_id: int, kind: str = "preference", scope_key: str | None = "acme") -> dict:
    return {"id": memory_id, "kind": kind, "scope_key": scope_key}


def turn(session: str, entry: str, form: str, *statements: dict) -> Turn:
    prompt = Prompt(session, entry, "claude-code", "acme", MONDAY, "a prompt")
    return Turn(prompt, Handout(form, list(statements)))


# Two sessions: each opens with three statements, and one later prompt of four
# is handed one statement.
TURNS = [
    turn("a", "a1", "opening", statement(1), statement(2, scope_key=None), statement(3)),
    turn("a", "a2", "match", statement(4, kind="correction")),
    turn("a", "a3", "match"),
    turn("b", "b1", "opening", statement(1), statement(2, scope_key=None), statement(5)),
    turn("b", "b2", "match"),
    turn("b", "b3", "match"),
]

LABELS = {
    Pair("a", "a1", 1): "good",
    Pair("a", "a1", 2): "noise",
    Pair("a", "a2", 4): "wrong",
    Pair("b", "b1", 2): "noise",
    # A pair this replay did not hand, from an earlier one, counts for nothing.
    Pair("c", "c1", 9): "good",
}


@pytest.fixture
def report():
    return summarize(TURNS, LABELS)


@pytest.mark.parametrize(
    "field, expected",
    [
        ("turns", 6),
        ("nothing", 3),
        ("statements", 7),
        ("unscoped", 2),
        ("labelled", 4),
    ],
)
def test_the_report_counts_the_turns_and_what_they_were_handed(report, field, expected):
    assert getattr(report, field) == expected


def test_the_report_counts_each_form_on_its_own(report):
    assert (report.forms["opening"].turns, report.forms["opening"].handed) == (2, 2)
    assert (report.forms["opening"].statements, report.forms["match"].statements) == (6, 1)
    assert (report.forms["match"].turns, report.forms["match"].handed) == (4, 1)


def test_the_report_counts_the_statements_by_kind(report):
    assert report.kinds == {"preference": 6, "correction": 1}


def test_the_report_counts_the_labels_of_the_pairs_it_handed(report):
    assert report.labels == {"good": 1, "noise": 2, "wrong": 1}


@pytest.mark.parametrize(
    "line",
    [
        "turns                   6",
        "handed nothing          3 of 6 (50.0%)",
        "statements per turn     1.17",
        "opening                 2 turns, 2 handed, 3.00 per handed turn",
        "match                   4 turns, 1 handed, 1.00 per handed turn",
        "no scope                2 of 7 (28.6%)",
        "  preference            6",
        "labelled                4 of 7 (57.1%)",
        "  noise                 2 (50.0%)",
    ],
)
def test_the_rendered_report_reads_as_a_table(report, line):
    assert line in render(report).splitlines()


def test_a_report_of_no_turns_renders(report):
    assert "turns                   0" in render(summarize([], {})).splitlines()


def test_the_pairs_are_the_prompt_and_each_statement_it_was_handed():
    assert pairs(TURNS[:2]) == [
        Pair("a", "a1", 1),
        Pair("a", "a1", 2),
        Pair("a", "a1", 3),
        Pair("a", "a2", 4),
    ]


def test_the_pairs_file_is_sorted_so_two_replays_diff(tmp_path: Path):
    written = tmp_path / "pairs.tsv"
    write_pairs(written, list(reversed(TURNS)))
    assert written.read_text().splitlines()[:2] == ["a\ta1\t1", "a\ta1\t2"]


@pytest.fixture
async def labelled(store: asyncpg.Pool) -> asyncpg.Pool:
    memory_id = await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint)
        VALUES ('Tests come before code here.', 'preference', 0.9, NULL, 's0', 'e0',
                'jev-1.13.0', 'fp')
        RETURNING id
        """
    )
    await store.execute(
        "INSERT INTO labels (session_id, entry_id, memory_id, label)"
        " VALUES ('a', 'a1', $1, 'good')",
        memory_id,
    )
    return store


async def test_the_labels_are_read_by_pair(labelled: asyncpg.Pool):
    memory_id = await labelled.fetchval("SELECT id FROM memories")
    assert await labels(labelled) == {Pair("a", "a1", memory_id): "good"}


async def test_a_store_without_the_labels_table_has_no_labels(store: asyncpg.Pool):
    await store.execute("DROP TABLE labels")
    assert await labels(store) == {}
