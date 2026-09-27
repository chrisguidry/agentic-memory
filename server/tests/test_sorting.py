"""Sorting a prospective statement into a decision, the state of the work, or a commitment.

These run against the store, because each outcome is a write to the row and
every read has to see it. The model is the narrowest fake that answers the
questions it is asked, and every statement is invented.
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from _sorting import FRIDAY_EVENING, SCOPE, FakeJudge, held, row_of
from typesafe_sdk import Noul, TypeSafeBadRequestError

from agentic_memory.classify import KIND_COLUMNS
from agentic_memory.db import SCHEMA
from agentic_memory.ledger import RecordedSystemOne
from agentic_memory.memories import end, memories, retire
from agentic_memory.settings import Settings
from agentic_memory.sorting import (
    QUESTIONS,
    moments,
    sort,
    sort_message,
    unsorted,
)

NEW_YORK = ZoneInfo("America/New_York")


class RefusingJudge:
    """A System One model that refuses every request, as the provider does over budget."""

    async def system_one(self, *, state, questions):
        raise TypeSafeBadRequestError(400, {"detail": {"error_type": "max_tokens_exceeded"}}, {})


async def sorted_by(store: asyncpg.Pool, judge) -> None:
    settings = Settings(time_zone="America/New_York")
    await sort_message(store, judge, session_id="s1", entry_id="e1", settings=settings)


class TestDecision:
    @pytest.mark.parametrize("kind", ["semantic", "procedural"])
    async def test_a_decision_is_written_again_as_the_kind_the_model_names(self, store, kind):
        prospective = await held(store, "The widget's release channel is an enum of two values.")
        await sorted_by(store, FakeJudge(decision=0.9, kind=kind))
        (found,) = await memories(store, scope_key=SCOPE)
        assert (found["statement"], found["kind"]) == (
            "The widget's release channel is an enum of two values.",
            kind,
        )
        assert (await row_of(store, prospective))["superseded_by"] == found["id"]

    async def test_the_decision_keeps_its_message_its_moment_and_its_answer(self, store):
        prospective = await held(store, "The widget's release channel is an enum of two values.")
        await sorted_by(store, FakeJudge(decision=0.9))
        before, after = (
            await row_of(store, prospective),
            await row_of(store, (await row_of(store, prospective))["superseded_by"]),
        )
        kept = ["scope_key", "session_id", "entry_id", "said_at", "actor", "actionable"]
        assert [after[column] for column in kept] == [before[column] for column in kept]

    async def test_a_decision_stays_prospective_when_its_message_wrote_that_kind(self, store):
        # One statement per message per kind, so the decision cannot be written
        # again as a fact beside the one its message wrote, and retiring it into
        # that fact would drop what it says.
        fact = await held(store, "The widget is written in Go.", kind="semantic")
        prospective = await held(store, "The widget will store its state in Postgres.")
        await sorted_by(store, FakeJudge(decision=0.9, kind="semantic"))
        found = await memories(store, scope_key=SCOPE)
        assert sorted((row["id"], row["kind"]) for row in found) == [
            (fact, "semantic"),
            (prospective, "prospective"),
        ]
        assert (await row_of(store, prospective))["sorted_at"] is not None

    async def test_a_decision_is_written_again_in_one_transaction(self, store):
        # A trigger refuses the retirement, so the write fails between the new
        # row and the pointer to it.
        await store.execute(
            """
            CREATE FUNCTION refuse_retirement() RETURNS trigger AS $$
            BEGIN
                IF NEW.superseded_by IS NOT NULL THEN
                    RAISE EXCEPTION 'retirement refused';
                END IF;
                RETURN NEW;
            END $$ LANGUAGE plpgsql;
            CREATE TRIGGER refuse_retirement BEFORE UPDATE ON memories
                FOR EACH ROW EXECUTE FUNCTION refuse_retirement();
            """
        )
        prospective = await held(store, "The widget's release channel is an enum of two values.")
        with pytest.raises(asyncpg.RaiseError):
            await sorted_by(store, FakeJudge(decision=0.9, kind="semantic"))
        assert [row["id"] for row in await memories(store, scope_key=SCOPE)] == [prospective]
        assert [row["id"] for row in await unsorted(store)] == [prospective]

    async def test_a_decision_is_not_ended_when_it_is_also_the_state_of_the_work(self, store):
        await held(store, "The widget's release channel is an enum of two values.")
        await sorted_by(store, FakeJudge(decision=0.9, state=0.9, kind="semantic"))
        assert [row["kind"] for row in await memories(store, scope_key=SCOPE)] == ["semantic"]

    async def test_a_commitment_that_reads_as_a_decision_stays_a_commitment(self, store):
        prospective = await held(store)
        await sorted_by(store, FakeJudge(commitment=0.9, decision=0.9, ends="monday"))
        found = await row_of(store, prospective)
        assert (found["kind"], found["superseded_by"]) == ("prospective", None)
        assert found["until_moment"] == moments(FRIDAY_EVENING, NEW_YORK)["monday"]


class TestStateOfTheWork:
    async def test_the_state_of_the_work_ends_with_nothing_to_replace_it(self, store):
        prospective = await held(store, "The widget branch is three commits ahead of its remote.")
        await sorted_by(store, FakeJudge(state=0.9))
        found = await row_of(store, prospective)
        assert (found["ended_reason"], found["superseded_by"]) == ("state", None)
        assert await memories(store, scope_key=SCOPE) == []

    async def test_a_commitment_is_not_ended_when_it_is_also_the_state_of_the_work(self, store):
        prospective = await held(store)
        await sorted_by(store, FakeJudge(commitment=0.9, state=0.9, ends="event"))
        assert (await row_of(store, prospective))["ended_at"] is None


# The moments a statement said late on a Friday evening in New York can end at.
# Each is an hour of the person's day, whatever UTC's day is then.
@pytest.mark.parametrize(
    "label, local",
    [
        ("today", datetime(2026, 9, 26, 0, 0)),
        ("tomorrow_morning", datetime(2026, 9, 26, 9, 0)),
        ("tomorrow", datetime(2026, 9, 27, 0, 0)),
        ("monday", datetime(2026, 9, 28, 9, 0)),
    ],
)
def test_each_moment_is_an_hour_of_the_persons_day(label, local):
    assert moments(FRIDAY_EVENING, NEW_YORK)[label] == local.replace(tzinfo=NEW_YORK)


def test_monday_said_on_a_monday_is_the_next_monday():
    said = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    assert moments(said, NEW_YORK)["monday"] == datetime(2026, 10, 5, 9, 0, tzinfo=NEW_YORK)


def test_a_morning_across_the_end_of_summer_time_is_still_nine():
    # New York leaves summer time early on Sunday 2026-11-01.
    said = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    assert moments(said, NEW_YORK)["monday"] == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


class TestCommitment:
    @pytest.mark.parametrize("label", ["today", "tomorrow_morning", "tomorrow", "monday"])
    async def test_a_commitment_ends_at_the_moment_the_model_names(self, store, label):
        prospective = await held(store)
        await sorted_by(store, FakeJudge(ends=label))
        found = await row_of(store, prospective)
        assert found["until_moment"] == moments(FRIDAY_EVENING, NEW_YORK)[label]
        assert found["until_event"] is None

    async def test_a_commitment_that_ends_on_an_event_holds_its_own_wording(self, store):
        prospective = await held(store, "The widget release is held until the review passes.")
        await sorted_by(store, FakeJudge(ends="event"))
        found = await row_of(store, prospective)
        assert (found["until_event"], found["until_moment"]) == (
            "The widget release is held until the review passes.",
            None,
        )

    async def test_a_commitment_with_no_end_stays_live_with_no_condition(self, store):
        prospective = await held(store, "The widget may move to a new store someday.")
        await sorted_by(store, FakeJudge(ends="none"))
        found = await row_of(store, prospective)
        assert (found["until_moment"], found["until_event"], found["ended_at"]) == (
            None,
            None,
            None,
        )

    async def test_a_commitment_is_not_sorted_again(self, store):
        await held(store)
        await sorted_by(store, FakeJudge(ends="none"))
        judge = FakeJudge()
        await sorted_by(store, judge)
        assert judge.asked == []

    async def test_a_commitment_with_no_moment_to_count_from_ends_on_no_moment(self, store):
        prospective = await held(store, said_at=None)
        await sorted_by(store, FakeJudge(ends="monday"))
        assert (await row_of(store, prospective))["until_moment"] is None


class TestWhatIsAsked:
    async def test_only_live_prospective_statements_of_the_message_are_asked(self, store):
        await held(store, "The widget is written in Go.", kind="semantic")
        await held(store, "Do not release the gadget before Monday.", entry_id="e2")
        ended = await held(store, "Do not release the widget before Tuesday.")
        await end(store, ended=[ended], reason="reread")
        judge = FakeJudge()
        await sorted_by(store, judge)
        assert judge.asked == []

    async def test_the_model_is_told_the_statement_and_the_exchange(self, store):
        await held(store)
        scores = dict.fromkeys(KIND_COLUMNS, 0.05) | {"prospective": 0.95}
        await store.execute(
            f"""
            INSERT INTO classifications
                (session_id, entry_id, model, questions_fingerprint, rounds, state,
                 {", ".join(scores)})
            VALUES ('s1', 'e1', 'jev-1.13.0', 'fp', 5, $1::jsonb,
                    {", ".join(f"${number}" for number in range(2, 2 + len(scores)))})
            """,
            json.dumps({"before": "[agent] ready to ship?", "message": "not before Monday"}),
            *scores.values(),
        )
        judge = FakeJudge()
        await sorted_by(store, judge)
        (state,) = judge.asked
        assert (state["statement"], state["before"], state["message"]) == (
            "Do not release the widget before Monday.",
            "[agent] ready to ship?",
            "not before Monday",
        )
        assert state["said"] == "2026-09-25 21:44 EDT, a Friday"

    async def test_a_refusal_leaves_the_statement_prospective_and_live(self, store):
        prospective = await held(store)
        await sorted_by(store, RefusingJudge())
        found = await row_of(store, prospective)
        assert (found["kind"], found["superseded_by"], found["ended_at"]) == (
            "prospective",
            None,
            None,
        )

    async def test_a_refused_statement_is_not_asked_again(self, store):
        # The provider refuses the same request again, so a statement it refused
        # would be at the head of every backfill's list and take its --limit.
        await held(store)
        await sorted_by(store, RefusingJudge())
        assert await unsorted(store) == []

    async def test_the_call_is_recorded_under_its_run(self, store):
        await held(store)
        recorded = RecordedSystemOne(FakeJudge(), store)
        await sort_message(
            store, recorded, session_id="s1", entry_id="e1", settings=Settings(), run="sort-1"
        )
        (row,) = await store.fetch("SELECT task, run, entry_id FROM model_calls")
        assert (row["task"], row["run"], row["entry_id"]) == ("sort", "sort-1", "e1")


@pytest.mark.parametrize("name", ["decision", "commitment", "state"])
def test_the_yes_or_no_questions_inspect_the_statement(name):
    assert isinstance(QUESTIONS[name], Noul)
    assert "`statement`" in QUESTIONS[name].instructions["question"]


@pytest.mark.parametrize(
    "name, labels",
    [
        ("kind", {"semantic", "procedural"}),
        ("ends", {"today", "tomorrow_morning", "tomorrow", "monday", "event", "none"}),
    ],
)
def test_the_choices_name_their_labels(name, labels):
    assert set(QUESTIONS[name].criteria) == labels


@pytest.mark.parametrize(
    "setting", ["sort_commitment", "sort_decision", "sort_state", "sort_moment"]
)
def test_every_threshold_is_a_probability_setting(setting):
    assert 0.0 < getattr(Settings(), setting) < 1.0


class FailingOnceJudge(FakeJudge):
    """A model whose first call fails the way a timeout or a 503 does."""

    def __init__(self, **answers):
        super().__init__(**answers)
        self.failed = False

    async def system_one(self, *, state, questions):
        if not self.failed:
            self.failed = True
            raise RuntimeError("the provider answered 503")
        return await super().system_one(state=state, questions=questions)


class TestBackfill:
    async def test_every_unsorted_statement_is_sorted(self, store):
        for number in range(5):
            await held(
                store, f"Do not release widget {number} before Monday.", entry_id=f"e{number}"
            )
        rows = await unsorted(store)
        count = await sort(store, FakeJudge(state=0.9), rows, settings=Settings(), concurrency=2)
        assert count == (5, 0)
        assert await unsorted(store) == []

    async def test_a_failure_is_counted_and_the_rest_are_sorted(self, store):
        for number in range(3):
            await held(
                store, f"Do not release widget {number} before Monday.", entry_id=f"e{number}"
            )
        rows = await unsorted(store)
        judge = FailingOnceJudge(state=0.9)
        assert await sort(store, judge, rows, settings=Settings()) == (2, 1)
        assert len(await unsorted(store)) == 1

    async def test_a_statement_retired_after_it_was_listed_stays_retired(self, store):
        prospective = await held(store, "The widget will store its state in Postgres.")
        newer = await held(store, "The widget will store its state in SQLite.", entry_id="e2")
        rows = [row for row in await unsorted(store) if row["id"] == prospective]
        await retire(store, replaced=[prospective], replacement=newer)
        await sort(store, FakeJudge(decision=0.9, kind="semantic"), rows, settings=Settings())
        statements = {row["statement"] for row in await memories(store, scope_key=SCOPE)}
        assert statements == {"The widget will store its state in SQLite."}

    async def test_a_statement_sorted_after_it_was_listed_is_not_sorted_again(self, store):
        prospective = await held(store)
        rows = await unsorted(store)
        await sorted_by(store, FakeJudge(commitment=0.9, ends="monday"))
        await sort(store, FakeJudge(decision=0.9, kind="semantic"), rows, settings=Settings())
        found = await row_of(store, prospective)
        assert (found["kind"], found["superseded_by"]) == ("prospective", None)

    async def test_the_oldest_statement_comes_first(self, store):
        first = await held(store, "first", entry_id="e1")
        second = await held(store, "second", entry_id="e2")
        assert [row["id"] for row in await unsorted(store)] == [first, second]


async def test_the_store_takes_the_new_reason_over_the_older_check(store):
    await store.execute("ALTER TABLE memories DROP CONSTRAINT memories_ended_reason_check")
    await store.execute(
        "ALTER TABLE memories ADD CONSTRAINT memories_ended_reason_check"
        " CHECK (ended_reason IN ('event', 'reread'))"
    )
    earlier = await held(store, "The widget is written in C.", entry_id="e0")
    await end(store, ended=[earlier], reason="reread")
    await store.execute(SCHEMA.read_text())
    await store.execute(SCHEMA.read_text())
    prospective = await held(store)
    assert await end(store, ended=[prospective], reason="state") == [prospective]
