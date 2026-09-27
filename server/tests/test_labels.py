"""Judging one turn's handout.

The stratified draw is pure, and is tested on its own. Everything that reads
or writes a pair runs against a real database, because the query is what
proves a pair is matched to the right prompt and a labelled pair is not
offered again. Every session, prompt, and statement here is invented.
"""

import random
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from agentic_memory.app import app
from agentic_memory.labels import (
    Pair,
    build_sample,
    candidate_pairs,
    next_unlabelled,
    stratified_sample,
    write_label,
)

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
) -> int:
    """One turn's handout, recorded the way `recall.record` writes it."""
    return await store.fetchval(
        "INSERT INTO injections (session_id, harness, scope_key, memory_ids, injected_at)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        session_id,
        harness,
        scope_key,
        memory_ids,
        injected_at,
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


class TestNextUnlabelled:
    async def filed(self, store: asyncpg.Pool) -> tuple[int, int]:
        first = await held(store, "rule one")
        second = await held(store, "rule two", entry_id="e2")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await said(store, "s1", "e2", NOW + timedelta(minutes=1), "and rollbacks?")
        await injected(store, "s1", NOW, [first])
        await injected(store, "s1", NOW + timedelta(minutes=1), [second])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        return first, second

    async def test_the_first_call_gives_the_first_pair(self, store: asyncpg.Pool):
        first, _ = await self.filed(store)
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"]["memory_id"] == first
        assert (found["done"], found["total"]) == (0, 2)

    async def test_a_labelled_pair_is_not_offered_again(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        shown = await next_unlabelled(store, sample="sample")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        found = await next_unlabelled(store, sample="sample", after=shown["pair"]["id"])
        assert found["pair"]["memory_id"] == second
        assert found["done"] == 1

    async def test_every_pair_labelled_offers_nothing(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        await write_label(store, session_id="s1", entry_id="e2", memory_id=second, label="noise")
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"] is None
        assert (found["done"], found["total"]) == (2, 2)

    async def test_a_skip_wraps_back_to_what_was_skipped(self, store: asyncpg.Pool):
        first, second = await self.filed(store)
        shown = await next_unlabelled(store, sample="sample")
        skipped = await next_unlabelled(store, sample="sample", after=shown["pair"]["id"])
        assert skipped["pair"]["memory_id"] == second
        wrapped = await next_unlabelled(store, sample="sample", after=skipped["pair"]["id"])
        assert wrapped["pair"]["memory_id"] == first

    async def test_a_pair_carries_the_prompt_and_the_statement(self, store: asyncpg.Pool):
        await self.filed(store)
        found = await next_unlabelled(store, sample="sample")
        assert found["pair"]["prompt"] == "how do we deploy?"
        assert found["pair"]["statement"] == "rule one"

    async def test_a_pair_labelled_under_another_sample_counts_as_done(self, store: asyncpg.Pool):
        first, _ = await self.filed(store)
        await build_sample(
            store, name="other", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )
        await write_label(store, session_id="s1", entry_id="e1", memory_id=first, label="good")
        found = await next_unlabelled(store, sample="other")
        assert found["done"] == 1


class TestWriteLabel:
    async def test_a_label_can_be_written(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="good")
        assert await store.fetchval("SELECT label FROM labels") == "good"

    async def test_a_second_label_replaces_the_first(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="good")
        await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="wrong")
        assert await store.fetchval("SELECT count(*) FROM labels") == 1
        assert await store.fetchval("SELECT label FROM labels") == "wrong"

    async def test_a_label_outside_the_three_kinds_is_refused(self, store: asyncpg.Pool):
        memory = await held(store, "a rule")
        with pytest.raises(asyncpg.CheckViolationError):
            await write_label(store, session_id="s1", entry_id="e1", memory_id=memory, label="meh")


@pytest.fixture
async def client(store: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    """The service, with a store of its own and nothing else running."""
    app.state.pool = store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    ) as calling:
        yield calling


class TestRoutes:
    async def test_the_next_route_and_the_label_route_agree_with_the_functions(
        self, client: httpx.AsyncClient, store: asyncpg.Pool
    ):
        memory = await held(store, "a rule")
        await said(store, "s1", "e1", NOW, "how do we deploy?")
        await injected(store, "s1", NOW, [memory])
        await build_sample(
            store, name="sample", since=NOW - timedelta(1), until=NOW + timedelta(1), size=10
        )

        first = await client.get("/labels/next", params={"sample": "sample"})
        assert first.status_code == 200
        pair = first.json()["pair"]
        assert pair["statement"] == "a rule"

        posted = await client.post(
            "/labels",
            json={
                "session_id": pair["session_id"],
                "entry_id": pair["entry_id"],
                "memory_id": pair["memory_id"],
                "label": "good",
            },
        )
        assert posted.status_code == 200

        done = await client.get("/labels/next", params={"sample": "sample"})
        body = done.json()
        assert body["pair"] is None
        assert body["done"] == 1
