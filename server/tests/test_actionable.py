"""Whether a statement would change what an agent does.

These run against the store, because the answer is written on the row and the
match reads it there. The model is the narrowest fake that answers the one
question, and every statement is invented.
"""

import json
from types import SimpleNamespace

import asyncpg
import pytest
from typesafe_sdk import TypeSafeBadRequestError

from agentic_memory.actionable import ACTIONABLE, answer, answer_message, price, unanswered
from agentic_memory.classify import KIND_COLUMNS, questions_fingerprint
from agentic_memory.embed import Embedder
from agentic_memory.ledger import RecordedSystemOne
from agentic_memory.settings import Settings
from agentic_memory.synthesize import synthesize


class FakeJudge:
    """A System One model that gives every statement the same answer."""

    def __init__(self, probability: float = 0.9):
        self.probability = probability
        self.asked: list[dict] = []

    async def system_one(self, *, state, questions):
        self.asked.append(state)
        return SimpleNamespace(
            model="jev-1.13.0",
            nouls={name: SimpleNamespace(noul=self.probability) for name in questions},
        )


class RefusingJudge:
    """A System One model that refuses every request, as the provider does over budget."""

    async def system_one(self, *, state, questions):
        raise TypeSafeBadRequestError(400, {"detail": {"error_type": "max_tokens_exceeded"}}, {})


async def held(
    store: asyncpg.Pool,
    statement: str,
    *,
    scope_key: str | None = "example.test/acme/widget",
    entry_id: str = "e1",
) -> int:
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint)
        VALUES ($1, 'semantic', 0.9, $2, 's1', $3, 'jev-1.13.0', 'fp')
        RETURNING id
        """,
        statement,
        scope_key,
        entry_id,
    )


async def answered(store: asyncpg.Pool, statement_id: int) -> float | None:
    return await store.fetchval("SELECT actionable FROM memories WHERE id = $1", statement_id)


class TestAnswerMessage:
    async def test_the_answer_is_written_on_the_row(self, store):
        written = await held(store, "The widget is written in Go.")
        await answer_message(store, FakeJudge(0.3), session_id="s1", entry_id="e1")
        assert await answered(store, written) == pytest.approx(0.3)

    @pytest.mark.parametrize(
        "scope_key, place",
        [("example.test/acme/widget", "example.test/acme/widget"), (None, "everywhere")],
    )
    async def test_the_model_is_told_the_statement_and_its_place(self, store, scope_key, place):
        await held(store, "Tests come before code.", scope_key=scope_key)
        judge = FakeJudge()
        await answer_message(store, judge, session_id="s1", entry_id="e1")
        assert judge.asked == [{"statement": "Tests come before code.", "place": place}]

    async def test_only_the_statements_of_that_message_are_asked(self, store):
        await held(store, "The widget is written in Go.", entry_id="e1")
        other = await held(store, "The gadget is written in Rust.", entry_id="e2")
        await answer_message(store, FakeJudge(), session_id="s1", entry_id="e1")
        assert await answered(store, other) is None

    async def test_a_statement_already_answered_is_not_asked_again(self, store):
        await held(store, "The widget is written in Go.")
        await answer_message(store, FakeJudge(), session_id="s1", entry_id="e1")
        judge = FakeJudge()
        await answer_message(store, judge, session_id="s1", entry_id="e1")
        assert judge.asked == []

    async def test_a_refusal_leaves_the_statement_unanswered(self, store):
        written = await held(store, "The widget is written in Go.")
        await answer_message(store, RefusingJudge(), session_id="s1", entry_id="e1")
        assert await answered(store, written) is None

    async def test_the_call_is_recorded_under_its_run(self, store):
        await held(store, "The widget is written in Go.")
        recorded = RecordedSystemOne(FakeJudge(), store)
        await answer_message(store, recorded, session_id="s1", entry_id="e1", run="backfill-1")
        (row,) = await store.fetch("SELECT task, run, entry_id FROM model_calls")
        assert (row["task"], row["run"], row["entry_id"]) == ("actionable", "backfill-1", "e1")


class FailingOnceJudge(FakeJudge):
    """A model whose first call fails the way a timeout or a 503 does."""

    def __init__(self):
        super().__init__()
        self.failed = False

    async def system_one(self, *, state, questions):
        if not self.failed:
            self.failed = True
            raise RuntimeError("the provider answered 503")
        return await super().system_one(state=state, questions=questions)


class TestBackfill:
    async def test_only_live_unanswered_statements_are_candidates(self, store):
        live = await held(store, "The widget is written in Go.", entry_id="e1")
        retired = await held(store, "The widget is written in C.", entry_id="e2")
        done = await held(store, "The widget ships weekly.", entry_id="e3")
        await store.execute(
            "UPDATE memories SET superseded_by = $1, superseded_at = now() WHERE id = $2",
            live,
            retired,
        )
        await store.execute("UPDATE memories SET actionable = 0.8 WHERE id = $1", done)
        assert [row["id"] for row in await unanswered(store)] == [live]

    async def test_every_candidate_is_answered(self, store):
        for number in range(5):
            await held(store, f"Rule {number} of the widget.", entry_id=f"e{number}")
        count = await answer(store, FakeJudge(), await unanswered(store), concurrency=2)
        assert count == (5, 0)
        assert await unanswered(store) == []

    async def test_a_failure_is_counted_and_the_rest_are_answered(self, store):
        for number in range(3):
            await held(store, f"Rule {number} of the widget.", entry_id=f"e{number}")
        count = await answer(store, FailingOnceJudge(), await unanswered(store))
        assert count == (2, 1)
        assert len(await unanswered(store)) == 1

    async def test_the_price_is_read_from_the_calls_already_made(self, store):
        await store.execute(
            """
            INSERT INTO model_calls (provider, model, task, input_tokens, output_tokens, outcome)
            VALUES ('typesafe', 'jev-1.13.0', 'actionable', 300, 10, 'ok'),
                   ('typesafe', 'jev-1.13.0', 'actionable', 500, 30, 'ok'),
                   ('typesafe', 'jev-1.13.0', 'merge', 9000, 9000, 'ok')
            """
        )
        assert await price(store, 10) == (4000, 200)

    async def test_nothing_is_priced_before_a_call_is_made(self, store):
        assert await price(store, 10) is None


def test_the_question_inspects_the_statement():
    assert ACTIONABLE.instructions["inspect"] == "`statement`"
    assert "`statement`" in ACTIONABLE.instructions["question"]


class Writer:
    """The model that writes the sentences, replying with one statement."""

    async def post(self, url, *, headers, json):
        reply = '[{"kind": "semantic", "statement": "The widget is written in Go."}]'
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": reply}}]},
        )


async def test_a_statement_is_answered_when_it_is_written(store, embedder: Embedder):
    scores = dict.fromkeys(KIND_COLUMNS, 0.05) | {"semantic": 0.95}
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, model, questions_fingerprint, rounds, state,
             {", ".join(scores)})
        VALUES ('s1', 'e1', 'jev-1.13.0', $1, 5, $2::jsonb,
                {", ".join(f"${number}" for number in range(3, 3 + len(scores)))})
        """,
        questions_fingerprint(),
        json.dumps({"before": "", "message": "the widget is in Go"}),
        *scores.values(),
    )
    await synthesize(
        "s1",
        "e1",
        settings=Settings(),
        pool=store,
        client=Writer(),
        embedder=embedder,
        judge=FakeJudge(0.7),
    )
    assert await store.fetchval("SELECT actionable FROM memories") == pytest.approx(0.7)
