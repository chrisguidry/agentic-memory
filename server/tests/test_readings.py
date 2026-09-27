"""Whether a prompt is about anything, judged by the prompts the classifier read.

These run against the store and the real embedding model, because the judgment
is a query over the vectors the model gave the prompts, and a fake vector would
prove nothing about which prompts are near one another. Every prompt here is
invented.
"""

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from agentic_memory.classify import KIND_COLUMNS
from agentic_memory.embed import QUERY_CHARACTERS, Embedder, trimmed
from agentic_memory.readings import embed_reading, embed_readings, holds_nothing, short
from agentic_memory.recall import choose
from agentic_memory.settings import Settings

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
NEIGHBOURS = Settings(recall_neighbours=5, recall_empty_share=0.6)


async def read(
    store: asyncpg.Pool,
    message: str,
    *,
    held_memory: bool,
    classified_at: datetime = NOW - timedelta(days=1),
    entry_id: str | None = None,
) -> None:
    """One reading of a prompt, which held a memory or held none."""
    scores = dict.fromkeys(KIND_COLUMNS, 0.05) | ({"semantic": 0.95} if held_memory else {})
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, model, questions_fingerprint, rounds, state,
             classified_at, {", ".join(scores)})
        VALUES ('s1', $1, 'jev-1.13.0', 'fp', 5, $2::jsonb, $3,
                {", ".join(f"${number}" for number in range(4, 4 + len(scores)))})
        """,
        entry_id or message,
        json.dumps({"before": "", "message": message}),
        classified_at,
        *scores.values(),
    )


# Replies that carry no subject of their own, and prompts that each held a
# memory about the work.
REPLIES = (
    "keep going",
    "looks good",
    "yes, do that",
    "ok, thanks",
    "sounds good to me",
    "great, carry on",
)
SUBJECTS = (
    "the tests always run against a real postgres container",
    "we use uv for python here, never pip",
    "commit messages say what changed and then why",
    "the deploy reads its secrets from the vault",
    "the media library scans its root once an hour",
    "the widget's config is in widget.toml at the repo root",
)


@pytest.fixture
async def readings(store: asyncpg.Pool, embedder: Embedder) -> asyncpg.Pool:
    for message in REPLIES:
        await read(store, message, held_memory=False)
    for message in SUBJECTS:
        await read(store, message, held_memory=True)
    await embed_readings(store, embedder)
    return store


async def quiet(store, embedder, prompt, settings=NEIGHBOURS, as_of=None) -> bool:
    return await holds_nothing(
        store, embedder.query(prompt), model=embedder.model, settings=settings, as_of=as_of
    )


class TestHoldsNothing:
    async def test_a_reply_among_replies_holds_nothing(self, readings, embedder):
        assert await quiet(readings, embedder, "looks great, keep going") is True

    async def test_a_prompt_with_a_subject_is_matched(self, readings, embedder):
        assert await quiet(readings, embedder, "how do the tests get a postgres database?") is False

    @pytest.mark.parametrize("cutoff, silent", [(0.0, True), (1.0, False)])
    async def test_the_cutoff_is_a_setting(self, readings, embedder, cutoff, silent):
        settings = Settings(recall_neighbours=5, recall_empty_share=cutoff)
        assert await quiet(readings, embedder, "looks great, keep going", settings) is silent

    async def test_too_few_readings_decide_nothing(self, readings, embedder):
        settings = Settings(recall_neighbours=50, recall_empty_share=0.0)
        assert await quiet(readings, embedder, "looks great, keep going", settings) is False

    async def test_a_reading_taken_after_the_moment_is_not_a_neighbour(self, readings, embedder):
        before_any = NOW - timedelta(days=2)
        found = await quiet(readings, embedder, "looks great, keep going", as_of=before_any)
        assert found is False

    async def test_a_reading_embedded_by_another_model_is_not_a_neighbour(self, readings, embedder):
        await readings.execute("UPDATE classifications SET prompt_embedding_model = 'older'")
        assert await quiet(readings, embedder, "looks great, keep going") is False


@pytest.mark.parametrize(
    "prompt, words, expected",
    [
        ("keep going", 2, True),
        ("keep going", 1, False),
        ("  keep   going\n", 2, True),
        ("", 0, True),
        ("take a writing pass through every doc in the repo", 12, True),
        ("take a writing pass through every doc in the repo", 9, False),
    ],
)
def test_a_prompt_is_short_up_to_the_word_limit(prompt, words, expected):
    assert short(prompt, words) is expected


class TestChoose:
    async def chosen(self, store, embedder, prompt, settings=NEIGHBOURS):
        found = await choose(
            store,
            embedder,
            settings,
            seen=frozenset(),
            scope_key="example.test/acme/widget",
            prompt=prompt,
            now=NOW,
        )
        return found.statements

    @pytest.fixture
    async def filed(self, readings: asyncpg.Pool) -> asyncpg.Pool:
        await readings.execute(
            """
            INSERT INTO memories
                (statement, kind, score, scope_key, session_id, entry_id, model,
                 questions_fingerprint, said_at)
            VALUES ('The widget is written in Go.', 'semantic', 0.9,
                    'example.test/acme/widget', 's0', 'e0', 'jev-1.13.0', 'fp', $1)
            """,
            NOW,
        )
        return readings

    async def test_a_first_prompt_that_holds_nothing_is_handed_nothing(self, filed, embedder):
        assert await self.chosen(filed, embedder, "looks great, keep going") == []

    async def test_a_first_prompt_with_a_subject_gets_the_list(self, filed, embedder):
        found = await self.chosen(filed, embedder, "how do the tests get a postgres database?")
        assert [row["statement"] for row in found] == ["The widget is written in Go."]

    # Sixteen words, all of them replies, so the neighbourhood is replies and
    # only the word limit decides.
    LONG_REPLY = (
        "looks great, keep going, sounds good to me, ok thanks, yes do that, great carry on"
    )

    @pytest.mark.parametrize("words, silenced", [(40, True), (16, True), (15, False), (3, False)])
    async def test_only_a_prompt_within_the_word_limit_is_silenced(
        self, filed, embedder, words, silenced
    ):
        settings = NEIGHBOURS.model_copy(update={"recall_silence_words": words})
        found = await self.chosen(filed, embedder, self.LONG_REPLY, settings)
        assert (found == []) is silenced


class TestTrimming:
    @pytest.mark.parametrize(
        "length, kept",
        [(0, 0), (10, 10), (QUERY_CHARACTERS, QUERY_CHARACTERS), (60_000, QUERY_CHARACTERS)],
    )
    def test_a_prompt_is_cut_to_the_characters_the_model_can_read(self, length, kept):
        assert len(trimmed("x" * length)) == kept

    # Two prompts over the limit: ordinary prose, and the longest words the
    # model's vocabulary holds whole, which put the most characters in each
    # token and so are the case the limit has to be safe for.
    @pytest.mark.parametrize(
        "text",
        [
            "The deploy reads its secrets from the vault, and the tests run in a container. " * 150,
            "telecommunications interdisciplinary telecommunication " * 250,
        ],
        ids=["prose", "long-words"],
    )
    def test_the_cut_leaves_the_vector_unchanged(self, embedder, text):
        assert len(text) > QUERY_CHARACTERS
        whole = next(iter(embedder._model.query_embed(text))).tolist()
        cut = embedder.query(text)
        assert sum(a * b for a, b in zip(whole, cut, strict=True)) == pytest.approx(1.0, abs=1e-5)

    def test_many_prompts_are_cut_the_same_way(self, embedder):
        text = "international understanding administration " * 400
        [together] = embedder.queries([text])
        alone = embedder.query(text)
        assert sum(a * b for a, b in zip(alone, together, strict=True)) == pytest.approx(1.0)


class TestEmbedding:
    def test_many_prompts_come_back_in_the_order_given(self, embedder):
        texts = ["the widget's config is in widget.toml at the repo root " * 20, "ok", "yes"]
        together = embedder.queries(texts)
        for text, vector in zip(texts, together, strict=True):
            alone = embedder.query(text)
            assert sum(a * b for a, b in zip(alone, vector, strict=True)) == pytest.approx(1.0)

    async def test_every_reading_is_embedded_with_the_model_named(self, store, embedder):
        await read(store, "keep going", held_memory=False)
        await read(store, "we use uv here", held_memory=True)
        assert await embed_readings(store, embedder) == 2
        models = await store.fetch("SELECT prompt_embedding_model FROM classifications")
        assert {row["prompt_embedding_model"] for row in models} == {embedder.model}

    async def test_a_reading_from_another_model_is_embedded_again(self, store, embedder):
        await read(store, "keep going", held_memory=False)
        await embed_readings(store, embedder)
        await store.execute("UPDATE classifications SET prompt_embedding_model = 'older'")
        assert await embed_readings(store, embedder) == 1

    async def test_a_reading_with_no_message_is_left_alone(self, store, embedder):
        await read(store, "", held_memory=False)
        assert await embed_readings(store, embedder) == 0

    async def test_the_task_embeds_the_reading_of_one_prompt(self, store, embedder):
        await read(store, "keep going", held_memory=False, entry_id="e1")
        await read(store, "looks good", held_memory=False, entry_id="e2")
        await embed_reading("s1", "e1", pool=store, embedder=embedder)
        embedded = await store.fetch(
            "SELECT entry_id FROM classifications WHERE prompt_embedding IS NOT NULL"
        )
        assert [row["entry_id"] for row in embedded] == ["e1"]
