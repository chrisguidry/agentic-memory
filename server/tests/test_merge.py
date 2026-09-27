"""Merging statements that say the same thing.

The cutoffs are what these pin. Two statements at or above the upper cutoff
merge on the number, in the band the model decides, and below the lower cutoff
nothing is compared. The statements carry hand-built unit vectors, so a
similarity is exactly the number the test chose, and the store is still the real
one pgvector runs the query in.
"""

from datetime import timedelta

import pytest
from _statements import ACROSS, BASE, CUTOFFS, MODEL, NOW, SCOPE, FakeJudge, at, held, live

from agentic_memory.ledger import RecordedSystemOne
from agentic_memory.match import match
from agentic_memory.memories import end, standing
from agentic_memory.merge import merge, merge_message
from agentic_memory.settings import Settings


class TestUpperCutoff:
    async def test_two_statements_at_the_upper_cutoff_leave_the_newer_one_standing(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        newer = await held(store, "Commits are not amended.", vector=at(0.96), said_at=NOW)
        await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"Commits are not amended."}
        pointed = await store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", older)
        assert pointed == newer

    async def test_the_model_is_not_asked_above_the_upper_cutoff(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are not amended.", vector=at(0.96), said_at=NOW)
        judge = FakeJudge()
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert judge.asked == []


class TestBand:
    async def test_the_band_merges_when_the_model_says_they_agree(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commit history is never rewritten.", vector=at(0.90), said_at=NOW)
        judge = FakeJudge(same=True)
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"Commit history is never rewritten."}
        assert judge.asked == [("Commits are never amended.", "Commit history is never rewritten.")]

    async def test_the_band_leaves_both_standing_when_the_model_says_they_differ(self, store):
        # An embedding cannot tell a negation from its opposite, which is why the
        # band is asked at all.
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are always amended.", vector=at(0.90), said_at=NOW)
        judge = FakeJudge(same=False)
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"Commits are never amended.", "Commits are always amended."}
        assert judge.asked == [("Commits are never amended.", "Commits are always amended.")]


class TestLowerCutoff:
    async def test_below_the_lower_cutoff_both_stand_and_the_model_is_not_asked(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "The media library scans hourly.", vector=at(0.50), said_at=NOW)
        judge = FakeJudge()
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert len(await live(store)) == 2
        assert judge.asked == []


class TestKinds:
    """One decision is often written once as praise and once as a fact.

    Merging across kinds is a setting, and it is off unless it is turned on.
    """

    @pytest.mark.parametrize("similarity", [0.99, 0.90])
    async def test_two_kinds_never_merge_by_default(self, store, similarity):
        older = await held(
            store, "Commits are never amended.", kind="preference", said_at=NOW - timedelta(hours=1)
        )
        await held(
            store,
            "Commit history is never rewritten.",
            kind="correction",
            vector=at(similarity),
            said_at=NOW,
        )
        judge = FakeJudge(same=True, settles=True)
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert len(await live(store)) == 2
        assert judge.asked == []

    @pytest.mark.parametrize("similarity", [0.99, 0.90])
    async def test_two_kinds_that_say_the_same_thing_leave_the_newer_standing(
        self, store, similarity
    ):
        older = await held(
            store,
            "The widget's first release went out on a signed tag, which worked well.",
            kind="praise",
            said_at=NOW - timedelta(hours=1),
        )
        newer = await held(
            store,
            "The widget's releases go out on a signed tag.",
            kind="semantic",
            vector=at(similarity),
            said_at=NOW,
        )
        await merge(store, FakeJudge(same=True), statement_id=older, model=MODEL, settings=ACROSS)
        assert await live(store) == {"The widget's releases go out on a signed tag."}
        survivor = await store.fetchrow("SELECT id, kind FROM memories WHERE superseded_by IS NULL")
        assert (survivor["id"], survivor["kind"]) == (newer, "semantic")

    async def test_praise_survives_when_it_was_said_last(self, store):
        await held(
            store,
            "The widget's releases go out on a signed tag.",
            kind="semantic",
            said_at=NOW - timedelta(hours=1),
        )
        newer = await held(
            store,
            "The signed-tag release of the widget went well.",
            kind="praise",
            vector=at(0.90),
            said_at=NOW,
        )
        await merge(store, FakeJudge(same=True), statement_id=newer, model=MODEL, settings=ACROSS)
        assert await live(store) == {"The signed-tag release of the widget went well."}

    async def test_two_kinds_that_differ_both_stand(self, store):
        older = await held(
            store, "The widget uses Postgres.", kind="semantic", said_at=NOW - timedelta(hours=1)
        )
        await held(
            store,
            "Moving the widget to Postgres went well.",
            kind="praise",
            vector=at(0.85),
            said_at=NOW,
        )
        await merge(store, FakeJudge(same=False), statement_id=older, model=MODEL, settings=ACROSS)
        assert len(await live(store)) == 2


class TestBoundary:
    async def test_two_scopes_never_merge(self, store):
        older = await held(
            store,
            "Commits are never amended.",
            scope_key="github.com/acme/widget",
            said_at=NOW - timedelta(hours=1),
        )
        await held(
            store,
            "Commit history is never rewritten.",
            scope_key="github.com/acme/other",
            vector=at(0.99),
            said_at=NOW,
        )
        await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS)
        assert len(await live(store, scope_key=None)) == 2

    async def test_two_scopes_that_disagree_both_stand(self, store):
        # A rule in two places is two statements, so one place changing its
        # rule leaves the other place's rule as it was.
        older = await held(
            store,
            "Releases go out on Fridays.",
            scope_key="github.com/acme/widget",
            said_at=NOW - timedelta(hours=1),
        )
        await held(
            store,
            "Releases go out on Mondays.",
            scope_key="github.com/acme/other",
            vector=at(0.90),
            said_at=NOW,
        )
        judge = FakeJudge(same=False, settles=True)
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert len(await live(store, scope_key=None)) == 2
        assert judge.asked == []

    async def test_a_null_scope_is_its_own_scope(self, store):
        older = await held(
            store, "Never use em dashes.", scope_key=None, said_at=NOW - timedelta(hours=1)
        )
        await held(store, "Do not use em dashes.", scope_key=None, vector=at(0.99), said_at=NOW)
        await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store, scope_key=None) == {"Do not use em dashes."}

    async def test_a_statement_embedded_by_another_model_is_not_a_candidate(self, store):
        await held(
            store,
            "Commits are never amended.",
            embedding_model="another-model",
            said_at=NOW - timedelta(hours=1),
        )
        newer = await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        await merge(store, FakeJudge(), statement_id=newer, model=MODEL, settings=CUTOFFS)
        assert len(await live(store)) == 2

    async def test_a_statement_embedded_by_another_model_is_not_compared(self, store):
        older = await held(
            store,
            "Commits are never amended.",
            embedding_model="another-model",
            said_at=NOW - timedelta(hours=1),
        )
        await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        assert (
            await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS) == []
        )

    async def test_a_statement_with_no_moment_is_never_compared(self, store):
        older = await held(store, "Commits are never amended.", said_at=None)
        await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        assert (
            await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS) == []
        )

    async def test_a_neighbour_with_no_moment_is_never_a_candidate(self, store):
        await held(store, "Commits are never amended.", said_at=None)
        newer = await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        await merge(store, FakeJudge(), statement_id=newer, model=MODEL, settings=CUTOFFS)
        assert len(await live(store)) == 2

    async def test_an_ended_statement_is_never_a_candidate(self, store):
        ended = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await end(store, ended=[ended], reason="reread")
        newer = await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        judge = FakeJudge()
        assert await merge(store, judge, statement_id=newer, model=MODEL, settings=CUTOFFS) == []
        assert (
            await store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", ended) is None
        )


class TestAbsent:
    async def test_a_merged_statement_is_gone_from_the_read_and_the_candidates(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        await merge(store, FakeJudge(), statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"Commits are not amended."}
        found = await standing(
            store,
            scope_key=SCOPE,
            kinds=["preference"],
            said_before=NOW + timedelta(hours=1),
        )
        assert [row["statement"] for row in found] == ["Commits are not amended."]

    async def test_a_merged_statement_is_not_a_candidate_for_the_match(self, store):
        retired = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are not amended.", vector=at(0.99), said_at=NOW)
        for number in range(10):
            await held(
                store,
                f"Filler {number}.",
                vector=at(0.1),
                said_at=NOW - timedelta(days=number + 2),
            )
        await merge(store, FakeJudge(), statement_id=retired, model=MODEL, settings=CUTOFFS)
        found = await match(
            store,
            BASE,
            model=MODEL,
            seen=(),
            scope_key=SCOPE,
            limit=5,
            margin=0.0,
            actionable=0.5,
            now=NOW,
        )
        statements = {row["statement"] for row in found}
        assert "Commits are not amended." in statements
        assert "Commits are never amended." not in statements


class TestMergeMessage:
    async def test_a_message_merges_the_statement_it_just_wrote(self, store):
        await held(
            store,
            "Commits are never amended.",
            entry_id="earlier",
            said_at=NOW - timedelta(hours=1),
        )
        await held(store, "Commits are not amended.", vector=at(0.99), entry_id="m1", said_at=NOW)
        merged = await merge_message(
            store,
            FakeJudge(),
            session_id="s1",
            entry_id="m1",
            model=MODEL,
            settings=CUTOFFS,
        )
        assert merged == 1
        assert await live(store) == {"Commits are not amended."}

    async def test_a_message_whose_statements_were_compared_is_not_asked_again(self, store):
        # The writer's task merges a message on every attempt, so a retry after a
        # failure finishes what the first attempt left, and a statement already
        # compared is not asked about twice.
        await held(
            store,
            "Commits are never amended.",
            entry_id="earlier",
            said_at=NOW - timedelta(hours=1),
        )
        await held(
            store,
            "Commit history is never rewritten.",
            vector=at(0.90),
            entry_id="m1",
            said_at=NOW,
        )
        judge = FakeJudge(same=False)
        await merge_message(
            store, judge, session_id="s1", entry_id="m1", model=MODEL, settings=CUTOFFS
        )
        await merge_message(
            store, judge, session_id="s1", entry_id="m1", model=MODEL, settings=CUTOFFS
        )
        assert len(judge.asked) == 1

    async def test_a_merge_call_is_recorded_as_a_merge(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commit history is never rewritten.", vector=at(0.90), said_at=NOW)
        recorded = RecordedSystemOne(FakeJudge(same=True), store)
        await merge(
            store,
            recorded,
            statement_id=older,
            model=MODEL,
            settings=CUTOFFS,
            run="september-backfill",
        )
        (row,) = await store.fetch("SELECT task, run, outcome FROM model_calls")
        assert (row["task"], row["run"], row["outcome"]) == ("merge", "september-backfill", "ok")


def test_both_cutoffs_and_the_second_question_are_settings():
    settings = Settings()
    assert 0 < settings.merge_lower < settings.merge_upper < 1
    assert 0 < settings.merge_settles < 1
    assert settings.merge_across_kinds is False
