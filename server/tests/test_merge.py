"""Merging statements that say the same thing.

The cutoffs are what these pin. Two statements at or above the upper cutoff
merge on the number, in the band the model decides, and below the lower cutoff
nothing is compared. The statements carry hand-built unit vectors, so a
similarity is exactly the number the test chose, and the store is still the real
one pgvector runs the query in.
"""

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentic_memory.embed import literal
from agentic_memory.match import match
from agentic_memory.memories import memories, standing
from agentic_memory.merge import merge, merge_backlog, merge_message
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "github.com/acme/widget"
MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# A unit vector at an angle from the first axis. Two of them have the cosine of
# the angle between them, so `at(s)` is at similarity `s` from `BASE` exactly.
BASE = [1.0] + [0.0] * (DIMENSIONS - 1)


def at(similarity: float) -> list[float]:
    angle = math.acos(similarity)
    return [math.cos(angle), math.sin(angle)] + [0.0] * (DIMENSIONS - 2)


CUTOFFS = Settings(embed_model=MODEL, merge_upper=0.95, merge_lower=0.80)


class FakeJudge:
    """A System One model that agrees or disagrees with every pair it is asked."""

    def __init__(self, agrees: bool = True):
        self.yes = agrees
        self.asked: list[tuple[str, str]] = []

    async def system_one(self, *, state, questions):
        self.asked.append((state["first"], state["second"]))
        return SimpleNamespace(nouls={"same": SimpleNamespace(noul=0.9 if self.yes else 0.1)})


class FixedEmbedder:
    """A model that answers with one vector, for a match the test controls."""

    model = MODEL

    def __init__(self, vector: list[float]):
        self.vector = vector

    def query(self, text: str) -> list[float]:
        return self.vector


async def held(
    store,
    statement: str,
    *,
    kind: str = "preference",
    scope_key: str | None = SCOPE,
    vector: list[float] | None = None,
    embedding_model: str = MODEL,
    said_at: datetime | None = NOW,
    session_id: str = "s1",
    entry_id: str | None = None,
) -> int:
    """One live statement with a vector the test chose."""
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, embedding, embedding_model)
        VALUES ($1, $2, 0.9, $3, $4, $5, 'jev-1.13.0', 'fp', $6, $7::vector, $8)
        RETURNING id
        """,
        statement,
        kind,
        scope_key,
        session_id,
        entry_id or statement,
        said_at,
        literal(vector or BASE),
        embedding_model,
    )


async def live(store, scope_key: str | None = SCOPE) -> set[str]:
    return {row["statement"] for row in await memories(store, scope_key=scope_key, now=NOW)}


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

    async def test_the_newest_of_a_group_is_the_survivor(self, store):
        # Every other member points straight at the survivor, so the count of
        # rows that point at it is the count of duplicates.
        oldest = await held(store, "A", said_at=NOW - timedelta(hours=3))
        middle = await held(store, "B", vector=at(0.99), said_at=NOW - timedelta(hours=2))
        newest = await held(store, "C", vector=at(0.98), said_at=NOW - timedelta(hours=1))
        await merge(store, FakeJudge(), statement_id=oldest, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"C"}
        assert (
            await store.fetchval("SELECT superseded_by FROM memories WHERE id = $1", middle)
            == newest
        )


class TestBand:
    async def test_the_band_merges_when_the_model_says_they_agree(self, store):
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commit history is never rewritten.", vector=at(0.90), said_at=NOW)
        judge = FakeJudge(agrees=True)
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert await live(store) == {"Commit history is never rewritten."}
        assert judge.asked == [("Commits are never amended.", "Commit history is never rewritten.")]

    async def test_the_band_leaves_both_standing_when_the_model_says_they_differ(self, store):
        # An embedding cannot tell a negation from its opposite, which is why the
        # band is asked at all.
        older = await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are always amended.", vector=at(0.90), said_at=NOW)
        judge = FakeJudge(agrees=False)
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


class TestBoundary:
    async def test_two_kinds_never_merge(self, store):
        older = await held(
            store, "Commits are never amended.", kind="preference", said_at=NOW - timedelta(hours=1)
        )
        await held(
            store,
            "Commit history is never rewritten.",
            kind="correction",
            vector=at(0.99),
            said_at=NOW,
        )
        judge = FakeJudge()
        await merge(store, judge, statement_id=older, model=MODEL, settings=CUTOFFS)
        assert len(await live(store)) == 2
        assert judge.asked == []

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
            FixedEmbedder(BASE),
            session_id="turn-1",
            scope_key=SCOPE,
            prompt="commits",
            limit=5,
            margin=0.0,
            now=NOW,
        )
        statements = {row["statement"] for row in found}
        assert "Commits are not amended." in statements
        assert "Commits are never amended." not in statements


class TestBacklog:
    async def test_the_pass_leaves_no_pair_above_the_upper_cutoff(self, store):
        await held(store, "A", said_at=NOW - timedelta(hours=3))
        await held(store, "B", vector=at(0.99), said_at=NOW - timedelta(hours=2))
        await held(store, "C", vector=at(0.98), said_at=NOW - timedelta(hours=1))
        await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(agrees=False))
        assert await live(store) == {"C"}

    async def test_the_pass_leaves_a_band_pair_the_model_rejected(self, store):
        await held(store, "Commits are never amended.", said_at=NOW - timedelta(hours=1))
        await held(store, "Commits are always amended.", vector=at(0.90), said_at=NOW)
        await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge(agrees=False))
        assert len(await live(store)) == 2

    async def test_only_the_model_being_compared_is_a_candidate(self, store):
        await held(store, "A", embedding_model="another-model", said_at=NOW - timedelta(hours=1))
        await held(store, "B", vector=at(0.99), embedding_model=MODEL, said_at=NOW)
        await merge_backlog(settings=CUTOFFS, pool=store, client=FakeJudge())
        assert len(await live(store)) == 2


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


def test_both_cutoffs_are_settings():
    settings = Settings()
    assert 0 < settings.merge_lower < settings.merge_upper < 1
