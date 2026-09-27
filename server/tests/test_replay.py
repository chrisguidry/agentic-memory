"""Replaying the record's prompts through the turn path.

These run against the store and the real embedding model, because a replay is
the turn path's own queries read at a moment in the past, and what it proves is
which statements those queries hand out. Every prompt and statement here is
invented.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.embed import Embedder, embed_missing
from agentic_memory.ingest import store as ingest
from agentic_memory.otlp import walk
from agentic_memory.replay.command import parser, run
from agentic_memory.replay.run import prompts, replay
from agentic_memory.settings import Settings

MONDAY = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
SCOPE = "example.test/acme/widget"
# Later than the wall clock the store stamps a record with on arrival, so every
# prompt a test writes has arrived by then.
LATER = datetime.now(UTC) + timedelta(days=1)


def text(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


async def said(
    store: asyncpg.Pool,
    session: str,
    entry: str,
    body: str,
    at: datetime,
    kind: str = "prompt",
    scope: str = SCOPE,
) -> None:
    """One prompt in the record, as a harness sends it."""
    record = {
        "timeUnixNano": str(int(at.timestamp() * 1_000_000_000)),
        "body": {"stringValue": body},
        "attributes": [
            text("session.id", session),
            text("agentic_memory.entry.id", entry),
            text("agentic_memory.kind", kind),
            text("gen_ai.agent.name", "claude-code"),
            text("agentic_memory.scope", scope),
        ],
    }
    payload = {"resourceLogs": [{"resource": {}, "scopeLogs": [{"logRecords": [record]}]}]}
    await ingest(store, list(walk(payload)))


async def held(
    store: asyncpg.Pool,
    statement: str,
    created: datetime,
    retired: datetime | None = None,
) -> int:
    """One statement, written at a moment and retired at another."""
    found = await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, created_at, actor, actor_depth)
        VALUES ($1, 'preference', 0.9, $2, 's0', $1, 'jev-1.13.0', 'fp', $3, $3, 'human', 0)
        RETURNING id
        """,
        statement,
        SCOPE,
        created,
    )
    if retired is not None:
        await store.execute(
            "UPDATE memories SET superseded_by = $1, superseded_at = $2 WHERE id = $1",
            found,
            retired,
        )
    return found


async def replayed(
    store: asyncpg.Pool,
    embedder: Embedder,
    until: datetime = MONDAY + timedelta(days=7),
    as_of: datetime = LATER,
    settings: Settings | None = None,
    excluded: tuple[str, ...] = (),
):
    found = await prompts(store, since=MONDAY, until=until, as_of=as_of, excluded=excluded)
    return await replay(store, embedder, settings or Settings(), found)


def handed(turn) -> list[str]:
    return [row["statement"] for row in turn.handout.statements]


@pytest.fixture
async def week(store: asyncpg.Pool) -> asyncpg.Pool:
    """Two sessions over a week, and a statement written before either."""
    await held(store, "Tests come before code here.", MONDAY - timedelta(days=1))
    await said(store, "a", "a1", "rename the widget", MONDAY + timedelta(hours=1))
    await said(store, "a", "a2", "now run the tests", MONDAY + timedelta(hours=2))
    await said(store, "b", "b1", "what is left to do?", MONDAY + timedelta(days=2))
    return store


async def test_a_sessions_first_prompt_is_the_opening_and_the_rest_are_matched(week, embedder):
    turns = await replayed(week, embedder)
    assert [(turn.prompt.entry_id, turn.handout.form) for turn in turns] == [
        ("a1", "opening"),
        ("a2", "match"),
        ("b1", "opening"),
    ]


async def test_a_replay_writes_no_injection(week, embedder):
    await replayed(week, embedder)
    assert await week.fetchval("SELECT count(*) FROM injections") == 0


@pytest.mark.parametrize(
    "body",
    [
        "<task-notification>a background task finished</task-notification>",
        "Base directory for this skill: /somewhere",
        "[Request interrupted by user]",
    ],
)
async def test_an_entry_the_harness_wrote_is_not_replayed(store, embedder, body):
    await said(store, "a", "a1", body, MONDAY)
    assert await replayed(store, embedder) == []


async def test_a_prompt_after_the_range_is_not_replayed(week, embedder):
    turns = await replayed(week, embedder, until=MONDAY + timedelta(days=1))
    assert [turn.prompt.entry_id for turn in turns] == ["a1", "a2"]


async def test_a_prompt_that_arrived_after_the_moment_is_not_replayed(week, embedder):
    assert await replayed(week, embedder, as_of=MONDAY + timedelta(days=3)) == []


@pytest.mark.parametrize(
    "excluded, expected",
    [
        ((), ["a1", "b1"]),
        (("example.test/acme/widget",), ["b1"]),
        (("example.test/acme",), ["b1"]),
        (("example.test/acme/wid",), ["a1", "b1"]),
        (("example.test/acme/widget", "example.test/other"), []),
    ],
    ids=["nothing", "the-scope", "above-it", "a-prefix-of-a-segment", "two-scopes"],
)
async def test_a_prompt_in_an_excluded_scope_is_not_replayed(store, embedder, excluded, expected):
    await said(store, "a", "a1", "rename the widget", MONDAY, scope="example.test/acme/widget")
    await said(store, "b", "b1", "tune the gadget", MONDAY, scope="example.test/other/gadget")
    turns = await replayed(store, embedder, excluded=excluded)
    assert [turn.prompt.entry_id for turn in turns] == expected


@pytest.mark.parametrize(
    "created, retired, expected",
    [
        (MONDAY - timedelta(days=1), None, ["the rule"]),
        (MONDAY + timedelta(days=1), None, []),
        (MONDAY - timedelta(days=1), MONDAY + timedelta(days=1), ["the rule"]),
        (MONDAY - timedelta(days=2), MONDAY - timedelta(days=1), []),
    ],
    ids=["written-before", "written-after", "retired-after", "retired-before"],
)
async def test_a_prompt_is_handed_what_was_live_when_it_was_said(
    store, embedder, created, retired, expected
):
    await held(store, "the rule", created, retired)
    await said(store, "a", "a1", "start", MONDAY)
    [turn] = await replayed(store, embedder)
    assert handed(turn) == expected


@pytest.fixture
async def embedded(store: asyncpg.Pool, embedder: Embedder) -> asyncpg.Pool:
    """Enough embedded statements for a match to have a baseline."""
    for n in range(12):
        await held(store, f"Rule {n} of the widget's tests.", MONDAY - timedelta(days=1))
    await embed_missing(store, embedder)
    await said(store, "a", "a1", "start", MONDAY)
    await said(store, "a", "a2", "which rules do the tests follow?", MONDAY + timedelta(hours=1))
    await said(store, "b", "b1", "which rules do the tests follow?", MONDAY + timedelta(hours=2))
    return store


# No margin, so every statement above the baseline is handed unless the
# session was already handed it.
OPEN_HANDED = Settings(recall_margin=0.0, recall_opening_limit=12)


async def test_what_a_session_was_handed_is_not_handed_to_it_again(embedded, embedder):
    first, second, _ = await replayed(embedded, embedder, settings=OPEN_HANDED)
    assert len(handed(first)) == 12
    assert handed(second) == []


async def test_another_session_is_handed_what_the_first_one_saw(embedded, embedder):
    _, _, other = await replayed(embedded, embedder, settings=OPEN_HANDED)
    assert len(handed(other)) == 12


async def test_the_command_prints_the_report_and_writes_the_pairs(week, postgres_url, tmp_path):
    written = tmp_path / "pairs.tsv"
    arguments = parser().parse_args(
        [
            "--since=2026-09-21",
            "--until=2026-09-28",
            f"--as-of={LATER.isoformat()}",
            f"--database-url={postgres_url}",
            f"--pairs={written}",
        ]
    )
    report = await run(arguments)
    rule = await week.fetchval("SELECT id FROM memories")
    assert "turns                   3" in report.splitlines()
    assert written.read_text().splitlines() == [f"a\ta1\t{rule}", f"b\tb1\t{rule}"]


def test_the_command_takes_more_than_one_scope_to_exclude():
    arguments = parser().parse_args(
        [
            "--since=2026-09-21",
            "--until=2026-09-28",
            "--as-of=2026-09-27",
            "--exclude-scope=example.test/acme",
            "--exclude-scope=example.test/other",
        ]
    )
    assert arguments.exclude_scope == ["example.test/acme", "example.test/other"]


async def test_the_command_leaves_out_an_excluded_scope(week, postgres_url):
    arguments = parser().parse_args(
        [
            "--since=2026-09-21",
            "--until=2026-09-28",
            f"--as-of={LATER.isoformat()}",
            f"--database-url={postgres_url}",
            f"--exclude-scope={SCOPE}",
        ]
    )
    assert "turns                   0" in (await run(arguments)).splitlines()
