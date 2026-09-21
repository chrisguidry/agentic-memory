"""A row for every model call the worker makes.

The row is what tells a deep backfill from ongoing use, so these pin the
context, the two providers' token names, and the outcomes. The wrappers sit
around the real store, and the fake clients underneath answer with what the
providers report.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from typesafe_sdk import TypeSafeBadRequestError

from agentic_memory.classify import KINDS, classify
from agentic_memory.embed import embed_missing
from agentic_memory.ledger import (
    RecordedCompletions,
    RecordedSystemOne,
    calling,
    calls,
    cursor_of,
    record,
    usage,
)
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def answered(
    model: str = "jev-1.13.0",
    request_id: str = "req_1",
    input_tokens: int = 120,
    output_tokens: int = 30,
) -> SimpleNamespace:
    """A System One answer with the metadata the provider reports."""
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        request_id=request_id,
    )


def reading(**scores) -> SimpleNamespace:
    """A System One answer with one probability per kind of memory."""
    nouls = {kind: SimpleNamespace(noul=value) for kind, value in scores.items()}
    return SimpleNamespace(**vars(answered()), nouls=nouls)


def completion(
    model: str = "deepseek-ai/DeepSeek-V4.1-Flash",
    input_tokens: int = 900,
    output_tokens: int = 40,
    cached: int | None = 800,
    reasoning: int | None = 12,
) -> dict:
    """A DeepInfra answer with the metadata the provider reports."""
    usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"id": "chatcmpl-abc", "model": model, "usage": usage}


class FakeSystemOne:
    """A System One client that answers with what it was handed, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def system_one(self, *, state, questions, **options):
        self.calls += 1
        result = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class FakeResponse:
    """An HTTP response with a status and a body."""

    def __init__(self, body: dict | None = None, status_code: int = 200):
        self.body = body or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=None, response=self)

    def json(self) -> dict:
        return self.body


class FakeHttp:
    """An httpx client that answers with what it was handed, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.asked: dict | None = None

    async def post(self, url, *, headers, json, **options):
        self.calls += 1
        self.asked = json
        result = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class FailingPool:
    """A store whose ledger write always fails."""

    async def execute(self, *args, **kwargs):
        raise RuntimeError("the ledger is down")


class ClassifyStore:
    """The three queries a window is built from, for a classify call."""

    def __init__(self, target, recent, before):
        self.target = target
        self.recent = recent
        self.before = before

    async def fetchrow(self, query, *values):
        return self.target

    async def fetch(self, query, *values):
        return self.recent if "ORDER BY occurred_at DESC" in query else self.before

    async def execute(self, query, *values):
        return "INSERT 0 1"


async def rows(store) -> list[dict]:
    return [dict(row) for row in await store.fetch("SELECT * FROM model_calls ORDER BY id")]


async def logged(
    store,
    *,
    provider: str = "typesafe",
    model: str = "jev-1.13.0",
    task: str = "classify",
    run: str = "live",
    input_tokens: int = 100,
    output_tokens: int = 10,
) -> None:
    """One row in the ledger, with the context set as a task would set it."""
    with calling(task, run=run):
        await record(
            store,
            provider=provider,
            model=model,
            duration_ms=5,
            outcome="ok",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class TestSystemOne:
    async def test_a_call_writes_a_row_with_the_versioned_model_and_tokens(self, store):
        client = RecordedSystemOne(FakeSystemOne(answered()), store)
        with calling("classify", session_id="s1", entry_id="e1", run="live"):
            await client.system_one(state={}, questions={})
        (row,) = await rows(store)
        assert (row["provider"], row["model"], row["task"]) == (
            "typesafe",
            "jev-1.13.0",
            "classify",
        )
        assert (row["session_id"], row["entry_id"], row["run"]) == ("s1", "e1", "live")
        assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
        assert row["request_id"] == "req_1"
        assert row["outcome"] == "ok"


class TestCompletions:
    async def test_a_call_writes_a_row_with_the_deepinfra_model_and_tokens(self, store):
        client = RecordedCompletions(FakeHttp(FakeResponse(completion())), store)
        with calling("synthesize", session_id="s1", entry_id="e1", run="live"):
            await client.post(
                "https://api.deepinfra.com/v1/openai/chat/completions", headers={}, json={}
            )
        (row,) = await rows(store)
        assert row["provider"] == "deepinfra"
        assert row["model"] == "deepseek-ai/DeepSeek-V4.1-Flash"
        assert row["request_id"] == "chatcmpl-abc"
        assert (row["input_tokens"], row["output_tokens"]) == (900, 40)
        assert row["cache_read_tokens"] == 800
        assert row["reasoning_tokens"] == 12

    async def test_a_provider_that_reports_no_cache_leaves_the_column_empty(self, store):
        body = completion(cached=None, reasoning=None)
        client = RecordedCompletions(FakeHttp(FakeResponse(body)), store)
        with calling("synthesize"):
            await client.post(
                "https://api.deepinfra.com/v1/openai/chat/completions", headers={}, json={}
            )
        (row,) = await rows(store)
        assert row["cache_read_tokens"] is None
        assert row["reasoning_tokens"] is None


class TestRefusal:
    async def test_a_refusal_writes_a_row_and_the_merge_question_answers_no(self, store):
        refused = TypeSafeBadRequestError(400, {"detail": {}}, {})
        client = RecordedSystemOne(FakeSystemOne(refused), store)
        with calling("merge", session_id="s1", entry_id="e1", run="live"):
            assert await _agrees(client) is False
        (row,) = await rows(store)
        assert row["outcome"] == "refused"
        assert row["error_type"] == "TypeSafeBadRequestError"
        assert row["task"] == "merge"

    async def test_a_http_refusal_writes_a_row_and_the_call_raises(self, store):
        client = RecordedCompletions(FakeHttp(FakeResponse(status_code=400)), store)
        with calling("synthesize"), pytest.raises(httpx.HTTPStatusError):
            await client.post(
                "https://api.deepinfra.com/v1/openai/chat/completions", headers={}, json={}
            )
        (row,) = await rows(store)
        assert row["outcome"] == "refused"
        assert row["error_type"] == "http_400"


async def _agrees(client) -> bool:
    from agentic_memory.merge import agrees

    return await agrees(client, "one", "two")


class TestRetries:
    async def test_each_attempt_writes_a_row(self, store):
        client = RecordedSystemOne(FakeSystemOne(RuntimeError("down"), answered()), store)
        with calling("classify"):
            with pytest.raises(RuntimeError):
                await client.system_one(state={}, questions={})
            await client.system_one(state={}, questions={})
        found = await rows(store)
        assert [row["outcome"] for row in found] == ["error", "ok"]
        assert found[0]["error_type"] == "RuntimeError"


class TestRun:
    async def test_a_named_run_travels_to_the_row(self, store):
        client = RecordedSystemOne(FakeSystemOne(answered()), store)
        with calling("classify", run="september-backfill"):
            await client.system_one(state={}, questions={})
        assert (await rows(store))[0]["run"] == "september-backfill"

    async def test_a_call_with_no_context_is_still_recorded(self, store):
        client = RecordedSystemOne(FakeSystemOne(answered()), store)
        await client.system_one(state={}, questions={})
        (row,) = await rows(store)
        assert row["run"] == "live"
        assert row["task"] == "unknown"

    async def test_the_classify_task_puts_its_run_in_the_row(self, store):
        # The run travels from the task argument, through the context, into the
        # row, so a backfill that schedules its own work totals under one name.
        target = {
            "occurred_at": NOW,
            "scope_key": "github.com/acme/widget",
            "body": "we use uv here",
            "actor": "person",
            "actor_depth": 0,
        }
        pool = ClassifyStore(target, [{"occurred_at": NOW}], [])
        inner = FakeSystemOne(reading(**dict.fromkeys(KINDS, 0.05)))
        client = RecordedSystemOne(inner, store)
        await classify(
            "s1",
            "e1",
            "september-backfill",
            settings=Settings(),
            pool=pool,
            client=client,
        )
        (row,) = await rows(store)
        assert row["task"] == "classify"
        assert row["run"] == "september-backfill"


class TestLedgerFailure:
    async def test_a_write_that_fails_is_logged_and_the_call_proceeds(self, caplog):
        client = RecordedSystemOne(FakeSystemOne(answered()), FailingPool())
        with caplog.at_level(logging.WARNING, logger="agentic_memory.ledger"):
            response = await client.system_one(state={}, questions={})
        assert response.model == "jev-1.13.0"
        assert "could not write a model call" in caplog.text


class TestUsage:
    async def test_the_totals_group_by_model(self, store):
        await logged(store, model="jev-1.13.0", input_tokens=100, output_tokens=10)
        await logged(
            store, provider="deepinfra", model="deepseek", input_tokens=900, output_tokens=40
        )
        found = {row["key"]: row for row in await usage(store, by="model")}
        assert found["jev-1.13.0"]["calls"] == 1
        assert found["deepseek"]["input_tokens"] == 900
        assert found["deepseek"]["output_tokens"] == 40

    async def test_the_totals_group_by_task(self, store):
        await logged(store, task="classify")
        await logged(store, task="merge")
        await logged(store, task="merge")
        found = {row["key"]: row for row in await usage(store, by="task")}
        assert found["merge"]["calls"] == 2

    async def test_the_totals_group_by_run(self, store):
        await logged(store, run="live")
        await logged(store, run="september-backfill", input_tokens=500, output_tokens=50)
        found = {row["key"]: row for row in await usage(store, by="run")}
        assert found["september-backfill"]["input_tokens"] == 500

    async def test_the_totals_group_by_day(self, store):
        await logged(store)
        await store.execute(
            "UPDATE model_calls SET called_at = called_at - interval '1 day' WHERE id = 1"
        )
        await logged(store)
        found = await usage(store, by="day")
        assert [row["calls"] for row in found] == [1, 1]

    async def test_a_range_leaves_out_calls_outside_it(self, store):
        await logged(store)
        await store.execute(
            "UPDATE model_calls SET called_at = called_at - interval '10 days' WHERE id = 1"
        )
        found = await usage(store, since=NOW, until=NOW + timedelta(days=1))
        assert len(found) == 0


class TestEmbedder:
    async def test_the_local_embedder_writes_no_row(self, store, embedder):
        await store.execute(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model, questions_fingerprint)
            VALUES ('Postgres is the store.', 'preference', 0.9, 's1', 'e1', 'jev-1.13.0', 'fp')
            """
        )
        assert await embed_missing(store, embedder) == 1
        assert await store.fetchval("SELECT count(*) FROM model_calls") == 0


class TestCalls:
    """The raw read an agent pulls, with no aggregation."""

    async def test_the_read_returns_the_calls_newest_first(self, store):
        await logged(store, task="classify")
        await logged(store, task="merge")
        assert [row["task"] for row in await calls(store)] == ["merge", "classify"]

    async def test_the_read_narrows_by_a_field(self, store):
        await logged(store, task="classify")
        await logged(store, task="merge")
        found = await calls(store, task="merge")
        assert [row["task"] for row in found] == ["merge"]

    async def test_the_read_narrows_by_a_range(self, store):
        await logged(store)
        await store.execute(
            "UPDATE model_calls SET called_at = called_at - interval '10 days' WHERE id = 1"
        )
        assert await calls(store, since=NOW) == []

    async def test_the_cursor_continues_without_repeating_a_row(self, store):
        await logged(store, task="classify")
        await logged(store, task="merge")
        await logged(store, task="synthesize")
        first = await calls(store, limit=2)
        second = await calls(store, limit=2, cursor=cursor_of(first[-1]))
        assert [row["id"] for row in first] + [row["id"] for row in second] == [3, 2, 1]
        assert not ({row["id"] for row in first} & {row["id"] for row in second})

    async def test_a_malformed_cursor_is_an_error(self, store):
        with pytest.raises(ValueError):
            await calls(store, cursor="not-a-cursor")
