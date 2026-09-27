"""Drawing a sample of prompt-and-statement pairs from the record.

The stratified draw is pure, and is tested on its own. Everything that reads
the record runs against a real database, because the query is what proves a
pair is matched to the right prompt. Every session, prompt, and statement
here is invented.
"""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.labels.sample import Pair, build_sample, candidate_pairs, stratified_sample

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "github.com/acme/widget"


def pair(n: int, form: str = "opening", kind: str = "semantic", scoped: bool = True) -> Pair:
    return Pair(
        session_id=f"s{n}",
        entry_id=f"e{n}",
        memory_id=n,
        form=form,
        kind=kind,
        scope_key=SCOPE if scoped else None,
    )


class TestStratifiedSample:
    def test_nothing_to_draw_from_gives_nothing(self):
        assert stratified_sample([], 10) == []

    def test_asking_for_more_than_there_is_returns_everything(self):
        pairs = [pair(n) for n in range(3)]
        assert len(stratified_sample(pairs, 10)) == 3

    def test_every_stratum_gets_an_even_share(self):
        pairs = [pair(n, form="opening") for n in range(10)] + [
            pair(n, form="match") for n in range(10, 20)
        ]
        chosen = stratified_sample(pairs, 6, rng=random.Random(0))
        assert Counter(found.form for found in chosen) == {"opening": 3, "match": 3}

    def test_a_spent_stratum_lets_the_others_take_more(self):
        pairs = [pair(0, form="opening")] + [pair(n, form="match") for n in range(1, 6)]
        chosen = stratified_sample(pairs, 4, rng=random.Random(0))
        assert Counter(found.form for found in chosen) == {"opening": 1, "match": 3}

    def test_kind_and_scope_are_their_own_strata(self):
        pairs = (
            [pair(n, kind="semantic", scoped=True) for n in range(0, 4)]
            + [pair(n, kind="semantic", scoped=False) for n in range(4, 8)]
            + [pair(n, kind="procedural", scoped=True) for n in range(8, 12)]
        )
        chosen = stratified_sample(pairs, 3, rng=random.Random(0))
        strata = {found.stratum for found in chosen}
        assert len(strata) == 3

    def test_the_draw_is_stable_under_a_seed(self):
        pairs = [pair(n) for n in range(20)]
        first = stratified_sample(pairs, 5, rng=random.Random(7))
        second = stratified_sample(pairs, 5, rng=random.Random(7))
        assert first == second


async def said(
    store: asyncpg.Pool,
    session_id: str,
    entry_id: str,
    occurred_at: datetime,
    body: str,
    actor: str = "human",
    actor_depth: int = 0,
) -> None:
    """One prompt, in the record, at a moment."""
    await store.execute(
        "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') ON CONFLICT DO NOTHING"
    )
    await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
    export_id = await store.fetchval(
        "INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)"
        " VALUES ($1, $2, (SELECT id FROM resources), (SELECT id FROM scopes), '{}')"
        " RETURNING id",
        session_id,
        entry_id,
    )
    await store.execute(
        "INSERT INTO logs (export_id, received_at, resource_id, scope_id, occurred_at,"
        " attributes, session_id, entry_id, kind, body, actor, actor_depth)"
        " VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), $2, '{}',"
        " $3, $4, 'prompt', $5, $6, $7)",
        export_id,
        occurred_at,
        session_id,
        entry_id,
        body,
        actor,
        actor_depth,
    )


async def held(
    store: asyncpg.Pool,
    statement: str,
    kind: str = "semantic",
    scope_key: str | None = SCOPE,
    entry_id: str | None = None,
) -> int:
    """One live statement, and its id."""
    return await store.fetchval(
        "INSERT INTO memories (statement, kind, score, scope_key, session_id, entry_id, model,"
        " questions_fingerprint, said_at, actor, actor_depth)"
        " VALUES ($1, $2, 0.9, $3, 's1', $4, 'jev-1.13.0', 'fp', now(), 'human', 0)"
        " RETURNING id",
        statement,
        kind,
        scope_key,
        entry_id or statement,
    )


async def injected(
    store: asyncpg.Pool,
    session_id: str,
    injected_at: datetime,
    memory_ids: list[int],
    harness: str = "pi",
    scope_key: str | None = SCOPE,
    form: str | None = None,
) -> int:
    """One turn's handout, recorded the way `recall.record` writes it.

    A row with no form is one written before the form was recorded.
    """
    return await store.fetchval(
        "INSERT INTO injections (session_id, harness, scope_key, memory_ids, injected_at, form)"
        " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        session_id,
        harness,
        scope_key,
        memory_ids,
        injected_at,
        form,
    )


class TestCandidatePairs:
    async def range(self, store, **kwargs):
        return await candidate_pairs(
            store, since=NOW - timedelta(days=1), until=NOW + timedelta(days=1), **kwargs
        )

    async def test_a_sessions_first_injection_is_the_opening_form(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        [found] = await self.range(store)
        assert found.form == "opening"

    async def test_a_later_injection_of_the_same_session_is_the_match_form(
        self, store: asyncpg.Pool
    ):
        first = await held(store, "a rule")
        second = await held(store, "another rule", entry_id="e2")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await said(store, "s1", "e2", NOW + timedelta(minutes=5), "and rollbacks?")
        await injected(store, "s1", NOW, [first])
        await injected(store, "s1", NOW + timedelta(minutes=5), [second])
        forms = {found.entry_id: found.form for found in await self.range(store)}
        assert forms == {"e1": "opening", "e2": "match"}

    async def test_a_first_turn_names_what_it_matched_apart_from_what_it_listed(
        self, store: asyncpg.Pool
    ):
        matched = await held(store, "a matched rule")
        listed = await held(store, "a listed rule", entry_id="e2")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [matched], form="match")
        await injected(store, "s1", NOW, [listed], form="opening")
        forms = {found.memory_id: found.form for found in await self.range(store)}
        assert forms == {matched: "match", listed: "opening"}

    async def test_the_pair_is_matched_to_the_prompt_said_just_before_it(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "an early question")
        await said(store, "s1", "e2", NOW + timedelta(seconds=2), "the real question")
        await injected(store, "s1", NOW + timedelta(seconds=3), [memory])
        [found] = await self.range(store)
        assert found.entry_id == "e2"

    async def test_a_plumbing_prompt_is_never_matched(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "<command-name>/clear</command-name>")
        await injected(store, "s1", NOW, [memory])
        assert await self.range(store) == []

    async def test_a_subagents_prompt_is_never_matched(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "do the subtask", actor="agent", actor_depth=1)
        await injected(store, "s1", NOW, [memory])
        assert await self.range(store) == []

    async def test_an_injection_with_no_prompt_near_it_is_left_out(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await injected(store, "s1", NOW, [memory])
        assert await self.range(store) == []

    async def test_an_injection_outside_the_range_is_left_out(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        found = await candidate_pairs(
            store, since=NOW + timedelta(days=1), until=NOW + timedelta(days=2)
        )
        assert found == []

    async def test_one_injection_produces_a_pair_per_statement(self, store: asyncpg.Pool):
        first = await held(store, "rule one")
        second = await held(store, "rule two", entry_id="e2")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [first, second])
        assert {found.memory_id for found in await self.range(store)} == {first, second}

    async def test_the_pair_carries_the_statements_kind_and_scope(self, store: asyncpg.Pool):
        await held(store, "a rule", kind="procedural", scope_key=None)
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        memory = await store.fetchval("SELECT id FROM memories")
        await injected(store, "s1", NOW, [memory])
        [found] = await self.range(store)
        assert (found.kind, found.scope_key) == ("procedural", None)

    @pytest.mark.parametrize(
        ("injection_scope", "excludes", "expect_included"),
        [
            ("github.com/acme/widget", ["github.com/acme/widget"], False),
            ("github.com/acme/widget/sub", ["github.com/acme/widget"], False),
            ("github.com/acme/widgetry", ["github.com/acme/widget"], True),
            ("github.com/acme/widget", [], True),
        ],
        ids=["excluded scope", "scope under it", "sibling sharing a prefix", "no option given"],
    )
    async def test_an_excluded_scope_is_filtered_in_the_query(
        self,
        store: asyncpg.Pool,
        injection_scope: str,
        excludes: list[str],
        expect_included: bool,
    ):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory], scope_key=injection_scope)
        found = await self.range(store, exclude_scopes=excludes)
        assert bool(found) == expect_included


class TestBuildSample:
    async def test_the_sample_is_stored_under_its_name(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        result = await build_sample(
            store, name="week-1", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        assert result == {"candidates": 1, "sampled": 1}
        assert await store.fetchval("SELECT count(*) FROM label_pairs WHERE sample = 'week-1'") == 1

    async def test_building_the_same_sample_twice_does_not_duplicate(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        for _ in range(2):
            await build_sample(
                store, name="week-1", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
            )
        assert await store.fetchval("SELECT count(*) FROM label_pairs WHERE sample = 'week-1'") == 1

    async def test_a_second_sample_can_draw_the_same_pair(self, store: asyncpg.Pool):
        # A pair is not the property of the sample that drew it, so nothing
        # stops two samples from holding the same one.
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="week-1", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        await build_sample(
            store, name="week-2", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        assert await store.fetchval("SELECT count(*) FROM label_pairs") == 2
