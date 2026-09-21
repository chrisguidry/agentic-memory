"""Turning a message into a sentence.

These cover the reply, which is the part that can fail quietly. A model that
wraps its answer in prose, or answers a question about a rule with a fact,
produces something that parses and is wrong.
"""

import json
from datetime import UTC, datetime
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

# When the message was said. The ranking ages a statement by this rather than
# by when the statement was written, so the writer records it on the row.
SAID = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def reading(**overrides) -> dict:
    """One classification row, as the store hands it back."""
    scores = dict.fromkeys(KIND_COLUMNS, 0.05)
    found = {
        "session_id": "s1",
        "entry_id": "e1",
        "scope_key": "github.com/liken-sh",
        "model": "jev-1.13.0",
        "questions_fingerprint": questions_fingerprint(),
        "before": "[person] earlier",
        "message": "we use uv, not pip",
        "beyond_this_project": 0.1,
        "forbids": 0.1,
        "said_at": SAID,
    }
    found.update(scores)
    found.update(overrides)
    return found


class FakeStore:
    """A store that answers the reading, the held statements, and the writes."""

    def __init__(self, found, standing: list[dict] | None = None):
        self.found = found
        self.standing = standing or []
        self.written: list[tuple] = []
        self.retired: list[tuple] = []
        self.asked_standing: tuple | None = None

    async def fetchrow(self, query, *values):
        return self.found

    async def fetch(self, query, *values):
        self.asked_standing = values
        return self.standing

    async def fetchval(self, query, *values):
        self.written.append(values)
        return len(self.written)

    async def execute(self, query, *values):
        self.retired.append(values)
        return "UPDATE 1"


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


def replying(*statements, replaces=None, everywhere=None) -> FakeModel:
    return FakeModel(
        json.dumps(
            [
                {
                    "kind": kind,
                    "statement": statement,
                    "replaces": replaces,
                    **({"everywhere": everywhere} if everywhere is not None else {}),
                }
                for kind, statement in statements
            ]
        )
    )


def held(statement_id: int, kind: str = "preference", statement: str = "Postgres is the store."):
    """One statement the place already holds, as the store hands it back."""
    return {
        "id": statement_id,
        "kind": kind,
        "statement": statement,
        "score": 0.9,
        "scope_key": "github.com/liken-sh",
        "session_id": "s0",
        "entry_id": "e0",
        "model": "jev-1.13.0",
        "said_at": SAID,
        "created_at": None,
    }


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

    async def test_the_statement_carries_the_moment_the_message_was_said(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The repo uses uv."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[8] == SAID

    async def test_a_statement_the_writer_marks_everywhere_is_scoped_to_nothing(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "Never use em dashes."), everywhere=True)
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] is None

    async def test_a_statement_the_writer_says_nothing_about_keeps_the_scope(self):
        # Keeping the project is the narrower of the two ways to be wrong, so a
        # sentence with no answer stays here.
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The schema is at server/schema.sql."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] == "github.com/liken-sh"

    async def test_a_message_that_reads_as_general_does_not_remove_the_scope_by_itself(self):
        # The classifier judges the message, and one message can hold a general
        # rule and a fact about this project at once, so the sentence decides.
        store = FakeStore(reading(semantic=0.95, beyond_this_project=0.95))
        model = replying(("semantic", "In liken-sh, spec.zones must declare zone2."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] == "github.com/liken-sh"

    async def test_a_general_rule_the_writer_marks_is_left_out_of_the_project(self):
        store = FakeStore(reading(semantic=0.95, beyond_this_project=0.95))
        model = replying(("semantic", "Design comes before implementation."), everywhere=True)
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[3] is None

    async def test_two_sentences_from_one_message_decide_their_own_scope(self):
        store = FakeStore(reading(semantic=0.95, procedural=0.95))
        model = FakeModel(
            json.dumps(
                [
                    {"kind": "semantic", "statement": "general", "everywhere": True},
                    {"kind": "procedural", "statement": "here", "everywhere": False},
                ]
            )
        )
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert [written[3] for written in store.written] == [None, "github.com/liken-sh"]

    async def test_a_message_that_reads_as_general_is_told_the_sentence_decides(self):
        store = FakeStore(reading(semantic=0.95, beyond_this_project=0.95))
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "Decide each sentence for itself" in model.asked

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


class TestReplacing:
    async def test_a_message_that_does_not_push_back_is_offered_nothing(self):
        store = FakeStore(reading(semantic=0.95), standing=[held(7)])
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.asked_standing is None
        assert store.retired == []

    async def test_a_message_that_corrects_something_older_opens_the_gate(self):
        # The gate is separate from the kinds, so a message can replace
        # something without the correction kind firing.
        store = FakeStore(reading(semantic=0.95, corrects_earlier=0.9), standing=[held(7)])
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.asked_standing is not None

    async def test_the_held_statements_are_asked_for_at_the_place_and_kind(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        scope_key, kinds, said_before, _ = store.asked_standing
        assert scope_key == "github.com/liken-sh"
        assert kinds == ["correction"]
        assert said_before == SAID

    async def test_the_held_statements_are_listed_for_the_writer(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "[1] (preference) Postgres is the store." in model.asked

    async def test_a_message_with_nothing_held_asks_without_a_list(self):
        store = FakeStore(reading(correction=0.95))
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "already holds" not in model.asked

    async def test_a_replacement_ends_the_statement_it_names(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "The store is Redis."), replaces=[1])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (retired,) = store.retired
        assert retired[0] == 7
        assert retired[1] == 1

    async def test_a_number_that_was_never_offered_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=[99])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_number_written_as_text_is_read(self):
        # The model answers with numbers and with the numbers as text, and both
        # mean the same thing.
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=["1"])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (retired,) = store.retired
        assert retired[0] == 7

    async def test_an_answer_that_is_not_a_number_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=["the first one"])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_replacement_that_was_not_a_list_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces="1")
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_statement_that_replaces_nothing_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []


class TestSynthesize:
    async def test_the_task_writes_what_the_writer_writes(self):
        store = FakeStore(reading(semantic=0.95))
        model = replying(("semantic", "The repo uses uv."))
        await synthesize("s1", "e1", settings=Settings(), pool=store, client=model)
        assert len(store.written) == 1


@pytest.mark.parametrize("kind", sorted(THRESHOLDS))
def test_every_threshold_is_a_probability(kind):
    assert 0.0 < THRESHOLDS[kind] < 1.0
