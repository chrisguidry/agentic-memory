"""Posting transcript lines to the service.

These run against a real database, because what the route proves is that a
file sent a chunk at a time lands in the store as the whole file does. Every
transcript here is invented.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import asyncpg
import httpx
import pytest

from agentic_memory.app import app
from agentic_memory.classify import task_key

SESSION = "11111111-2222-3333-4444-555555555555"
ROLLOUT = "99999999-8888-7777-6666-555555555555"
CWD = "/home/someone/src/example.test/acme/widget"
SCOPE = "example.test/acme/widget"
MACHINE = "a-test-machine"
PATH = f"/sessions/{SESSION}.jsonl"


class Scheduler:
    """A docket that keeps the readings the service asked for.

    The real one needs Redis, and what these tests check is what the route
    stored. The name of each scheduled reading is kept so a test can say that
    the prompts in a chunk were handed on.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []

    def add(self, task: Callable, key: str) -> Callable:
        self.keys.append(key)

        async def scheduled(*arguments: Any) -> None:
            return None

        return scheduled


@pytest.fixture
def scheduler() -> Scheduler:
    return Scheduler()


@pytest.fixture
async def client(store: asyncpg.Pool, scheduler: Scheduler) -> AsyncIterator[httpx.AsyncClient]:
    """The service, with a store of its own and nothing else running."""
    app.state.pool = store
    app.state.docket = scheduler
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as calling:
        yield calling


def chunk(lines: list[str], **overrides: Any) -> dict[str, Any]:
    """The body `POST /v1/transcripts` takes."""
    return {
        "harness": "claude-code",
        "machine": MACHINE,
        "path": PATH,
        "cwd": CWD,
        "version": "2.1.278",
        "scope": SCOPE,
        "scope_kind": "repo",
        "entries_before": 0,
        "lines": lines,
        **overrides,
    }


async def post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/v1/transcripts", json=body)


async def entry_ids(store: asyncpg.Pool) -> list[str]:
    found = await store.fetch("SELECT entry_id FROM logs ORDER BY id")
    return [row["entry_id"] for row in found]


def entry(kind: str, content: Any, *, uuid: str) -> str:
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "sessionId": SESSION,
            "cwd": CWD,
            "version": "2.1.278",
            "timestamp": "2026-09-21T12:00:00.000Z",
            "message": {"role": kind, "content": content},
        }
    )


def bookkeeping(kind: str) -> str:
    """An entry with no id and no working directory, named by its place."""
    return json.dumps({"type": kind, "sessionId": SESSION, "value": kind})


@pytest.fixture
def session_lines() -> list[str]:
    return [
        entry("user", "please rename the widget", uuid="u1"),
        bookkeeping("mode"),
        entry(
            "assistant",
            [
                {"type": "text", "text": "Renaming it."},
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"path": "widget.py"}},
            ],
            uuid="a1",
        ),
        entry(
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "renamed"}],
            uuid="u2",
        ),
        bookkeeping("last-prompt"),
        entry("assistant", [{"type": "text", "text": "Done."}], uuid="a2"),
    ]


def in_chunks(lines: list[str], *sizes: int) -> Iterator[dict[str, Any]]:
    """The same lines as the client sends them, a chunk at a time."""
    sent = 0
    for size in sizes:
        yield chunk(lines[sent : sent + size], entries_before=sent)
        sent += size
    assert sent == len(lines)


class TestChunks:
    async def test_chunks_store_what_the_whole_file_stores(
        self, client: httpx.AsyncClient, store: asyncpg.Pool, session_lines: list[str]
    ):
        for part in in_chunks(session_lines, 2, 3, 1):
            assert (await post(client, part)).status_code == 200

        from_chunks = await entry_ids(store)
        assert from_chunks == [
            "u1:prompt",
            f"{SESSION}:line-1:bookkeeping",
            "a1:response",
            "a1:tool_call:t1",
            "u2:tool_result",
            f"{SESSION}:line-4:bookkeeping",
            "a2:response",
        ]

        whole = await post(client, chunk(session_lines))
        assert whole.json()["inserted"] == 0
        assert await entry_ids(store) == from_chunks

    async def test_a_chunk_with_no_working_directory_keeps_the_sessions(
        self, client: httpx.AsyncClient, store: asyncpg.Pool
    ):
        await post(client, chunk([entry("user", "hello", uuid="u1")]))
        await post(client, chunk([bookkeeping("mode")], entries_before=1))
        found = await store.fetch("SELECT working_directory FROM logs ORDER BY id")
        assert [row["working_directory"] for row in found] == [CWD, CWD]


REPOSITORY = {
    "name": "widget",
    "owner": "acme",
    "url": "git@example.test:acme/widget.git",
    "branch": "main",
    "revision": "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3",
}


class TestTheRepository:
    """The service has no checkout, so it stores the one the client read."""

    async def test_a_chunk_with_a_repository_stores_it(
        self, client: httpx.AsyncClient, store: asyncpg.Pool, session_lines: list[str]
    ):
        await post(client, chunk(session_lines, repository=REPOSITORY))
        found = await store.fetchrow(
            "SELECT repository, owner, revision FROM logs ORDER BY id LIMIT 1"
        )
        assert dict(found) == {
            "repository": REPOSITORY["url"],
            "owner": REPOSITORY["owner"],
            "revision": REPOSITORY["revision"],
        }

    async def test_a_chunk_with_no_repository_stores_nothing(
        self, client: httpx.AsyncClient, store: asyncpg.Pool, session_lines: list[str]
    ):
        await post(client, chunk(session_lines))
        found = await store.fetchrow(
            "SELECT repository, owner, revision FROM logs ORDER BY id LIMIT 1"
        )
        assert dict(found) == {"repository": None, "owner": None, "revision": None}


class TestTheAnswer:
    async def test_the_counts_say_what_was_stored(
        self, client: httpx.AsyncClient, session_lines: list[str]
    ):
        answer = await post(client, chunk(session_lines))
        assert answer.status_code == 200
        assert answer.json() == {"received": 7, "inserted": 7, "repeated": 0}

    async def test_the_same_lines_again_are_counted_as_repeats(
        self, client: httpx.AsyncClient, session_lines: list[str]
    ):
        await post(client, chunk(session_lines))
        answer = await post(client, chunk(session_lines))
        assert answer.json() == {"received": 7, "inserted": 0, "repeated": 7}

    async def test_the_prompts_are_handed_to_the_classifier(
        self, client: httpx.AsyncClient, scheduler: Scheduler, session_lines: list[str]
    ):
        await post(client, chunk(session_lines))
        assert scheduler.keys == [task_key(SESSION, "u1:prompt")]


class TestWhatIsRefused:
    async def test_an_unknown_harness_is_refused(self, client: httpx.AsyncClient):
        answer = await post(client, chunk([], harness="a-harness-nobody-wrote"))
        assert answer.status_code == 400
        assert "a-harness-nobody-wrote" in answer.json()["detail"]


def rollout(*lines: dict[str, Any]) -> list[str]:
    return [json.dumps(found) for found in lines]


@pytest.fixture
def codex_lines() -> list[str]:
    return rollout(
        {
            "type": "session_meta",
            "timestamp": "2026-09-21T12:00:00.000Z",
            "payload": {
                "id": ROLLOUT,
                "session_id": SESSION,
                "cwd": CWD,
                "cli_version": "0.5.0",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-21T12:00:01.000Z",
            "payload": {
                "id": "m1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "please rename the widget"}],
            },
        },
    )


class TestCodex:
    async def test_a_whole_file_is_read(
        self, client: httpx.AsyncClient, store: asyncpg.Pool, codex_lines: list[str]
    ):
        answer = await post(client, chunk(codex_lines, harness="codex"))
        assert answer.status_code == 200
        assert await entry_ids(store) == ["m1:prompt"]

    async def test_a_chunk_from_the_middle_is_refused(
        self, client: httpx.AsyncClient, codex_lines: list[str]
    ):
        answer = await post(client, chunk(codex_lines[1:], harness="codex", entries_before=1))
        assert answer.status_code == 400
        assert "read from its first entry" in answer.json()["detail"]
