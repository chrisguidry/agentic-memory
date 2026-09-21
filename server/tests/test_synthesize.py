"""Turning a message into a sentence.

These cover the reply, which is the part that can fail quietly. A model that
wraps its answer in prose, or answers a question about a rule with a fact,
produces something that parses and is wrong.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from agentic_memory.classify import KIND_COLUMNS, questions_fingerprint
from agentic_memory.settings import Settings
from agentic_memory.synthesize import (
    THRESHOLDS,
    ask,
    parse,
    synthesize,
    worth_writing,
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
        "actor": "person",
        "actor_depth": 0,
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
        self.limit = json["max_tokens"]
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

    async def test_the_statement_names_who_said_the_message(self):
        # Trust ranks a person's statement above an agent's, and a reader of the
        # list is told which it is. Neither is possible if the row does not name
        # the source.
        store = FakeStore(reading(semantic=0.95, actor="person", actor_depth=0))
        model = replying(("semantic", "The repo uses uv."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[9] == "person"
        assert written[10] == 0

    async def test_a_statement_from_an_agent_records_the_depth(self):
        # A prompt an orchestrator wrote for a subagent is depth one, and a
        # statement from it is the agent's words and not the person's.
        store = FakeStore(reading(semantic=0.95, actor="agent", actor_depth=2))
        model = replying(("semantic", "The repo uses uv."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[9] == "agent"
        assert written[10] == 2

    async def test_a_statement_with_no_recorded_actor_records_none(self):
        # An actor the harness did not report is an empty field and not a
        # guess, so a statement is not silently attributed to the person.
        store = FakeStore(reading(semantic=0.95, actor=None, actor_depth=None))
        model = replying(("semantic", "The repo uses uv."))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (written,) = store.written
        assert written[9] is None
        assert written[10] is None

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


async def said(store: asyncpg.Pool, entry_id: str, at: datetime | None) -> None:
    """The record of one message, said at a moment, or at none."""
    # The maps every record points at, made once. A statement cannot read a
    # row its own CTE inserted, so these are two statements of their own.
    await store.execute(
        "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') ON CONFLICT DO NOTHING"
    )
    await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
    export_id = await store.fetchval(
        """
        INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)
        VALUES ('s1', $1, (SELECT id FROM resources), (SELECT id FROM scopes), '{}')
        RETURNING id
        """,
        entry_id,
    )
    await store.execute(
        """
        INSERT INTO logs
            (export_id, received_at, resource_id, scope_id, occurred_at, attributes,
             session_id, entry_id, kind)
        VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',
                's1', $3, 'prompt')
        """,
        export_id,
        at,
        entry_id,
    )


async def read(store: asyncpg.Pool, entry_id: str, classified_at: datetime, **scores) -> None:
    """One reading that cleared the correction threshold, read at a moment."""
    columns = dict.fromkeys(KIND_COLUMNS, 0.05) | {"correction": 0.9} | scores
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, model, questions_fingerprint, rounds, state,
             classified_at, {", ".join(columns)})
        VALUES ('s1', $1, 'jev-1.13.0', $2, 5, '{{}}', $3,
                {", ".join(f"${number}" for number in range(4, 4 + len(columns)))})
        """,
        entry_id,
        questions_fingerprint(),
        classified_at,
        *columns.values(),
    )


class TestWorthWriting:
    """The writer takes the oldest message first.

    The order is the message's moment in the record and not the reading's,
    because a backfill reads history in whatever order its tasks finish.
    """

    @pytest.fixture
    async def out_of_order(self, store: asyncpg.Pool) -> asyncpg.Pool:
        # Read newest first, which is the order a backfill produces.
        await said(store, "winter", SAID - timedelta(days=200))
        await said(store, "spring", SAID - timedelta(days=100))
        await said(store, "today", SAID)
        await said(store, "unplaced", None)
        await read(store, "today", classified_at=SAID + timedelta(seconds=1))
        await read(store, "unplaced", classified_at=SAID + timedelta(seconds=2))
        await read(store, "spring", classified_at=SAID + timedelta(seconds=3))
        await read(store, "winter", classified_at=SAID + timedelta(seconds=4))
        return store

    async def test_the_oldest_message_is_written_first(self, out_of_order):
        found = await worth_writing(out_of_order)
        assert [entry for _, entry in found] == ["winter", "spring", "today", "unplaced"]

    async def test_a_message_that_cleared_no_threshold_is_not_written(self, store):
        await said(store, "quiet", SAID)
        await read(store, "quiet", classified_at=SAID, correction=0.1)
        assert await worth_writing(store) == []

    async def test_a_message_already_written_is_not_written_again(self, out_of_order):
        await out_of_order.execute(
            """
            INSERT INTO memories
                (statement, kind, score, session_id, entry_id, model, questions_fingerprint)
            VALUES ('done', 'correction', 0.9, 's1', 'winter', 'jev-1.13.0', $1)
            """,
            questions_fingerprint(),
        )
        found = await worth_writing(out_of_order)
        assert "winter" not in [entry for _, entry in found]


class TestRetry:
    def test_a_sentence_is_tried_again_when_the_provider_fails(self):
        retry = inspect.signature(synthesize).parameters["retry"].default
        assert retry.attempts > 1


class TestAsk:
    async def test_the_reply_has_room_for_a_long_answer(self):
        model = FakeModel("[]")
        await ask(model, Settings(), "anything")
        assert model.limit >= 2000

    async def test_the_answer_is_asked_for_on_one_line(self):
        model = replying(("semantic", "The store is Postgres."))
        await write(
            "s1", "e1", settings=Settings(), pool=FakeStore(reading(semantic=0.9)), client=model
        )
        assert "on one line" in model.asked
