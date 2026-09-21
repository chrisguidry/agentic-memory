"""Turning a message into a sentence.

These cover the reply, which is the part that can fail quietly. A model that
wraps its answer in prose, or answers a question about a rule with a fact,
produces something that parses and is wrong.
"""

import json
from types import SimpleNamespace

import pytest

from agentic_memory.classify import KIND_COLUMNS, questions_fingerprint
from agentic_memory.settings import Settings
from agentic_memory.synthesize import (
    THRESHOLDS,
    parse,
    synthesize,
    write,
    writing_from,
)


def reading(**overrides) -> dict:
    """One classification row, as the store hands it back."""
    scores = dict.fromkeys(KIND_COLUMNS, 0.05)
    found = {
        "session_id": "s1",
        "entry_id": "e1",
        "scope_key": "github.com/liken-sh",
        "model": "jev-1.13.0",
        "questions_fingerprint": questions_fingerprint(),
        "state": {"before": "[person] earlier", "message": "we use uv, not pip"},
        "beyond_this_project": 0.1,
        "forbids": 0.1,
    }
    found.update(scores)
    found.update(overrides)
    return found


class FakeStore:
    """A store that answers the reading and records the statements written."""

    def __init__(self, found):
        self.found = found
        self.written: list[tuple] = []

    async def fetchrow(self, query, *values):
        return self.found

    async def fetch(self, query, *values):
        return []

    async def execute(self, query, *values):
        self.written.append(values)


class FakeModel:
    """A model that replies with whatever it was handed."""

    def __init__(self, reply: str):
        self.reply = reply
        self.asked: str | None = None

    async def post(self, url, *, headers, json):
        self.asked = json["messages"][1]["content"]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": self.reply}}]},
        )


def replying(*statements) -> FakeModel:
    return FakeModel(json.dumps([{"kind": k, "statement": s} for k, s in statements]))


class TestParse:
    def test_a_plain_array_is_read(self):
        found = parse('[{"kind": "semantic", "statement": "The repo uses uv."}]')
        assert found == [{"kind": "semantic", "statement": "The repo uses uv."}]

    def test_an_array_wrapped_in_prose_is_still_read(self):
        found = parse(
            'Sure! Here it is:\n[{"kind": "semantic", "statement": "x"}]\nHope that helps.'
        )
        assert found == [{"kind": "semantic", "statement": "x"}]

    def test_an_empty_array_is_read_as_nothing(self):
        assert parse("[]") == []

    def test_an_answer_with_no_array_is_nothing(self):
        assert parse("I could not find anything worth remembering.") == []

    def test_an_answer_that_is_not_json_is_nothing(self):
        assert parse("[this is not json]") == []


class TestWritingFrom:
    def test_a_kind_that_produces_a_statement_does(self):
        assert writing_from("semantic")

    def test_a_kind_about_what_a_statement_is_about_does_not(self):
        # Those questions steer the writing rather than producing a statement of
        # their own.
        assert not writing_from("beyond_this_project")
        assert not writing_from("about_artifact")
        assert not writing_from("forbids")


class TestWrite:
    async def test_a_message_below_every_threshold_writes_nothing(self):
        store = FakeStore(reading())
        model = replying(("semantic", "should not be written"))
        written = await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert written == []
        assert model.asked is None

    async def test_a_sentence_is_written_for_the_kind_that_fired(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The repo uses uv, not pip."))
        written = await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert written == ["The repo uses uv, not pip."]

    async def test_a_sentence_for_a_kind_that_did_not_fire_is_dropped(self):
        # The model is told which kinds to write for. One it adds is a defect,
        # not a statement.
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "kept"), ("procedural", "dropped"))
        written = await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert written == ["kept"]

    async def test_the_statement_carries_the_score_that_produced_it(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The repo uses uv."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[1] == "semantic"
        assert written[2] == 0.95

    async def test_a_statement_that_holds_everywhere_is_scoped_to_nothing(self):
        store = FakeStore(reading(semantic=0.95, beyond_this_project=0.9))
        model = replying(("semantic", "Never use em dashes."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] is None

    async def test_a_statement_about_this_project_keeps_the_scope(self):
        store = FakeStore(reading(semantic=0.95, beyond_this_project=0.1))
        model = replying(("semantic", "The schema is at server/schema.sql."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] == "github.com/liken-sh"

    async def test_the_message_and_what_came_before_are_both_put_in_the_ask(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "we use uv, not pip" in model.asked
        assert "[person] earlier" in model.asked

    async def test_a_rule_not_to_break_is_named_as_one_in_the_ask(self):
        store = FakeStore(reading(correction=0.95, forbids=0.9))
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "rules something out" in model.asked

    async def test_the_reading_is_looked_up_by_the_question_set_that_made_it(self):
        store = FakeStore(None)
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.written == []


class TestSynthesize:
    async def test_the_task_writes_what_the_writer_writes(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The repo uses uv."))
        await synthesize("s1", "e1", settings=Settings(), pool=store, client=model)
        assert len(store.written) == 1


@pytest.mark.parametrize("kind", sorted(THRESHOLDS))
def test_every_threshold_is_a_probability(kind):
    assert 0.0 < THRESHOLDS[kind] < 1.0
