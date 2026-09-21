"""Handing a turn the statements that are about its prompt.

These run against the store and the real embedding model, because the match
is a query over vectors the model produced and a fake vector would prove
nothing about which statement is nearest.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.embed import Embedder, embed_message, embed_missing, literal, load
from agentic_memory.match import baseline, match
from agentic_memory.recall import handed, turn
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SCOPE = "github.com/acme/widget"


@pytest.fixture(scope="session")
def embedder() -> Embedder:
    """The real model, loaded once for the whole run."""
    return load(Settings())


async def held(
    store: asyncpg.Pool,
    statement: str,
    kind: str = "preference",
    scope_key: str | None = SCOPE,
    age_in_days: float = 0.0,
    entry_id: str | None = None,
) -> int:
    return await store.fetchval(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, actor, actor_depth)
        VALUES ($1, $2, 0.9, $3, 's1', $4, 'jev-1.13.0', 'fp', $5, 'human', 0)
        RETURNING id
        """,
        statement,
        kind,
        scope_key,
        entry_id or statement,
        NOW - timedelta(days=age_in_days),
    )


# The statements a prompt is compared with. A scope needs a hundred or more of
# them before the ninety-ninth percentile means anything, so the corpus is the
# handful the prompts are about plus a hundred about other things, built from
# ten subjects and ten predicates.
ABOUT = (
    "The test suite runs against a real Postgres container, one per worker.",
    "Commit messages say what changed and then why, in plain prose.",
    "The kitchen thermostat is set to 68 degrees overnight.",
    "Secrets for the deploy are read from the vault, never from the tree.",
    "The media library scans its root once an hour.",
)
SUBJECTS = (
    "The garden hose",
    "The invoice folder",
    "The bicycle's rear tire",
    "The printer on the second floor",
    "The dog's leash",
    "The porch light",
    "The recycling bin",
    "The piano",
    "The car's spare key",
    "The winter coat",
)
PREDICATES = (
    "is kept on the hook by the back door.",
    "is checked on the first of the month.",
    "needs attention before the weekend.",
    "was replaced last spring.",
    "belongs to the neighbour.",
    "is put away after dark.",
    "goes out on Tuesdays.",
    "has not been used since the move.",
    "is in the drawer under the stairs.",
    "was a gift from an aunt.",
)
FILLER = tuple(f"{subject} {predicate}" for subject in SUBJECTS for predicate in PREDICATES)
CORPUS = ABOUT + FILLER


@pytest.fixture(scope="session")
def vectors(embedder: Embedder) -> dict[str, list[float]]:
    """The corpus embedded once for the whole run, because it is 105 sentences."""
    return dict(zip(CORPUS, embedder.documents(list(CORPUS)), strict=True))


@pytest.fixture
async def filed(store: asyncpg.Pool, embedder: Embedder, vectors) -> asyncpg.Pool:
    for statement in CORPUS:
        row = await held(store, statement)
        await store.execute(
            "UPDATE memories SET embedding = $2::vector, embedding_model = $3 WHERE id = $1",
            row,
            literal(vectors[statement]),
            embedder.model,
        )
    return store


async def matched(store, embedder, prompt, session_id="turn-1", limit=5, margin=0.05):
    found = await match(
        store,
        embedder,
        session_id=session_id,
        scope_key=SCOPE,
        prompt=prompt,
        limit=limit,
        margin=margin,
        now=NOW,
    )
    return [row["statement"] for row in found]


class TestMatch:
    async def test_a_prompt_about_a_statement_is_handed_it(self, filed, embedder):
        found = await matched(filed, embedder, "how do the tests talk to postgres?")
        assert found[0] == ABOUT[0]

    async def test_a_prompt_about_nothing_in_the_record_is_handed_nothing(self, filed, embedder):
        assert await matched(filed, embedder, "what rhymes with orange?") == []

    async def test_a_statement_the_session_saw_is_not_handed_again(self, filed, embedder):
        first = await filed.fetchval("SELECT id FROM memories WHERE statement = $1", ABOUT[0])
        await filed.execute(
            "INSERT INTO injections (session_id, harness, scope_key, memory_ids)"
            " VALUES ('turn-1', 'pi', $1, $2)",
            SCOPE,
            [first],
        )
        assert ABOUT[0] not in await matched(filed, embedder, "how do the tests talk to postgres?")

    async def test_the_handout_never_exceeds_its_limit(self, filed, embedder):
        await held(filed, "The test database is copied from a template for each test.")
        await held(filed, "Tests that need a database ask the fixture for a fresh one.")
        found = await matched(filed, embedder, "how do the tests get a database?", limit=1)
        assert len(found) == 1

    async def test_a_statement_without_an_embedding_is_not_a_candidate(self, filed, embedder):
        await held(filed, "The tests connect to Postgres on a port the kernel hands out.")
        found = await matched(filed, embedder, "how do the tests talk to postgres?")
        assert "the kernel hands out" not in " ".join(found)

    async def test_a_statement_embedded_by_another_model_is_not_a_candidate(self, filed, embedder):
        await filed.execute(
            "UPDATE memories SET embedding_model = 'some-other-model' WHERE statement = $1",
            ABOUT[0],
        )
        assert ABOUT[0] not in await matched(filed, embedder, "how do the tests talk to postgres?")

    async def test_a_scope_of_a_few_statements_has_no_baseline(self, store, embedder):
        await held(store, ABOUT[0])
        await held(store, ABOUT[1])
        await embed_missing(store, embedder)
        assert await matched(store, embedder, "how do the tests talk to postgres?") == []

    async def test_a_fresh_statement_outranks_an_old_one_that_matches_as_well(
        self, filed, embedder
    ):
        await held(filed, "Tests get a fresh Postgres database each.", age_in_days=400.0)
        await held(filed, "Tests get a fresh Postgres database each, from a template.")
        await embed_missing(filed, embedder)
        found = await matched(filed, embedder, "how do the tests get a fresh postgres database?")
        assert found[0].endswith("from a template.")


class TestBaseline:
    def test_a_large_scope_uses_its_ninety_ninth_percentile(self):
        similarities = [i / 10000 for i in range(10000)]
        assert baseline(similarities) == pytest.approx(0.99, abs=0.001)

    def test_a_small_scope_uses_its_tenth_best(self):
        similarities = [i / 100 for i in range(50)]
        assert baseline(similarities) == pytest.approx(0.39)

    def test_ten_statements_or_fewer_have_no_baseline(self):
        assert baseline([i / 10 for i in range(10)]) is None
        assert baseline([]) is None


class TestTurn:
    async def asked(self, store, embedder, prompt, session_id="turn-1"):
        found = await turn(
            store,
            embedder,
            Settings(recall_session_limit=3),
            session_id=session_id,
            harness="pi",
            scope_key=SCOPE,
            prompt=prompt,
            now=NOW,
        )
        return [row["statement"] for row in found]

    async def test_a_sessions_first_ask_is_handed_the_top_of_the_list(self, filed, embedder):
        found = await self.asked(filed, embedder, "what rhymes with orange?")
        assert len(found) == 3

    async def test_a_later_ask_is_handed_only_what_matches(self, filed, embedder):
        await self.asked(filed, embedder, "hello")
        found = await self.asked(filed, embedder, "how do the tests talk to postgres?")
        assert found[0] == ABOUT[0]

    async def test_a_later_ask_with_nothing_relevant_is_handed_nothing(self, filed, embedder):
        await self.asked(filed, embedder, "hello")
        assert await self.asked(filed, embedder, "what rhymes with orange?") == []

    async def test_both_forms_are_recorded(self, filed, embedder):
        # The opening list is the newest three, so the oldest statement, about
        # the tests, is still unseen when the second ask is about it.
        await self.asked(filed, embedder, "hello")
        await self.asked(filed, embedder, "how do the tests talk to postgres?")
        assert len(await handed(filed, session_id="turn-1")) == 2

    async def test_an_empty_prompt_after_the_first_ask_is_handed_nothing(self, filed, embedder):
        await self.asked(filed, embedder, "hello")
        assert await self.asked(filed, embedder, "   ") == []


class TestEmbedding:
    async def test_the_statements_of_one_message_are_embedded_after_a_write(self, store, embedder):
        await held(store, "one", entry_id="e1")
        await held(store, "two", entry_id="e1", kind="semantic")
        await held(store, "three", entry_id="e2")
        assert await embed_message(store, embedder, "s1", "e1") == 2
        assert await store.fetchval("SELECT count(*) FROM memories WHERE embedding IS NULL") == 1

    async def test_the_model_is_written_beside_the_vector(self, store, embedder):
        await held(store, "one")
        await embed_missing(store, embedder)
        assert await store.fetchval("SELECT embedding_model FROM memories") == embedder.model

    async def test_a_row_from_another_model_is_embedded_again(self, store, embedder):
        await held(store, "one")
        await embed_missing(store, embedder)
        await store.execute("UPDATE memories SET embedding_model = 'older'")
        assert await embed_missing(store, embedder) == 1

    async def test_the_settings_name_the_model_and_the_limits(self):
        settings = Settings()
        assert settings.embed_model and settings.embed_threads > 0
        assert settings.recall_session_limit > settings.recall_prompt_limit > 0
        assert 0 < settings.recall_margin < 1
