"""Embedding statements and prompts with a local model, so they can be compared.

The model runs in the process through fastembed, on the CPU, and is loaded
once per process because loading it takes a second and a prompt takes twelve
milliseconds. The worker embeds each statement when the writer writes it, and
the service embeds each prompt as it arrives.

A vector from one model means nothing to another, so every row records the
model that embedded it and a row embedded by another model is embedded again.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from docket import Depends, Shared
from fastembed import TextEmbedding

from .db import store_pool
from .memories import live
from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.embed")

BATCH = 256

# The most characters of a prompt the model is given. bge-small reads at most
# 512 tokens and drops the rest, but it tokenizes the whole text first, and a
# pasted file of 60,000 characters took 0.3 seconds to tokenize for nothing.
# The longest word the vocabulary holds as one token is 18 characters, so with
# the space after it a token of prose covers at most 19, and 512 of them at
# most 9,728 characters. The tokenizer reads a word of more than 100 characters,
# such as a hash or a line of base64, as one unknown token, so 512 tokens of
# those cover far more than 10,000 characters and the cut changes the vector.
# Such a prompt is a pasted blob, and the tokens past the cut are more of it.
QUERY_CHARACTERS = 10_000

# The most tokens one run of the model is given, counted after the run is padded
# to its longest text. ONNX Runtime keeps the most memory any run has needed
# for as long as the process lives, so this sets the peak of a backfill and of
# a worker. Embedded in batches of 16, 3,840 prompts of 3 to 600 words took a
# process to 971 MB. With this budget the same prompts peaked at 375 MB, of
# which 307 MB is the process with the model loaded. A budget of 2,048 peaked
# at 455 MB and was no faster.
TOKENS = 1024


def batches(lengths: Sequence[int], budget: int) -> list[list[int]]:
    """The indices of prompts of these token lengths, grouped into runs of the model.

    The prompts go shortest first, so each run holds prompts of about one length
    and pads little. A run takes prompts while their count times the longest of
    them is within the budget, and a prompt over the budget on its own is a run
    of one.
    """
    order = sorted(range(len(lengths)), key=lambda index: lengths[index])
    found: list[list[int]] = []
    for index in order:
        if found and (len(found[-1]) + 1) * lengths[index] <= budget:
            found[-1].append(index)
        else:
            found.append([index])
    return found


def trimmed(text: str) -> str:
    """A prompt cut to the characters the model can read."""
    return text[:QUERY_CHARACTERS]


class Embedder:
    """One loaded model, and the ways to use it.

    `lock` lets one run of the model at a time, in the worker and in the api. A
    worker runs up to ten tasks at once, each embedding in a thread of its own,
    and ONNX Runtime keeps the memory of every run that overlapped: prompts of
    512 tokens, ten at a time, took a process to 582 MB and it stayed there. One
    at a time, the same prompts peaked at 343 MB, of which 307 MB is the process
    with the model loaded. A prompt waits for the run ahead of it, and a run of
    512 tokens took about 150 ms on a busy laptop.
    """

    def __init__(self, model: str, threads: int, cache: str) -> None:
        self.model = model
        self.lock = threading.Lock()
        self._model = TextEmbedding(model_name=model, threads=threads, cache_dir=cache)

    def documents(self, texts: list[str]) -> list[list[float]]:
        """The vectors for statements, in the order given."""
        return self._runs(texts, self._model.embed)

    def query(self, text: str) -> list[float]:
        """The vector for a prompt, cut to the characters the model can read.

        fastembed embeds a query and a document of bge-small the same way, so
        this differs from `documents` only in the cut.
        """
        with self.lock:
            return next(iter(self._model.query_embed(trimmed(text)))).tolist()

    def queries(self, texts: list[str]) -> list[list[float]]:
        """The vectors for many prompts, in the order given, each as `query` makes it."""
        return self._runs([trimmed(text) for text in texts], self._model.query_embed)

    def tokens(self, texts: Sequence[str]) -> list[int]:
        """How many tokens the model reads of each text, after its own truncation."""
        encoded = self._model.model.tokenizer.encode_batch(list(texts))
        return [sum(encoding.attention_mask) for encoding in encoded]

    def _runs(self, texts: Sequence[str], embed: Callable[..., Iterable[Any]]) -> list[list[float]]:
        """The vectors for these texts, in runs of at most `TOKENS` padded tokens.

        A run is padded to its longest text, and prompts run from two words to
        a pasted file, so one long prompt among short ones made a run fifteen
        times slower than embedding each prompt alone, and many long ones made
        it hold hundreds of megabytes. The texts go shortest first, and the
        vectors are put back in the order given.
        """
        vectors: list[list[float]] = [[] for _ in texts]
        for run in batches(self.tokens(texts), TOKENS):
            with self.lock:
                found = embed([texts[index] for index in run], batch_size=len(run))
                for index, vector in zip(run, found, strict=True):
                    vectors[index] = vector.tolist()
        return vectors


def load(settings: Settings) -> Embedder:
    return Embedder(settings.embed_model, settings.embed_threads, settings.embed_cache)


@asynccontextmanager
async def shared_embedder():
    """The model, loaded once and shared by every task on a worker."""
    yield await asyncio.to_thread(load, get_settings())


# The live statements this model has not embedded. A row embedded by another
# model counts as unembedded, because its vector is in another space.
UNEMBEDDED = f"""
    SELECT id, statement
    FROM memories
    WHERE {live()}
      AND (embedding IS NULL OR embedding_model IS DISTINCT FROM $1)
      AND ($2::text IS NULL OR (session_id = $2 AND entry_id = $3))
    ORDER BY id
    LIMIT $4
"""

# A new vector clears the mark the merge leaves, because the statement was
# compared under the old vector, and a model change would otherwise leave every
# statement marked and never compared under the new one.
STORE = """
    UPDATE memories
    SET embedding = $2::vector, embedding_model = $3, merged_at = NULL, merged_by_run = NULL
    WHERE id = $1
"""


def literal(vector: list[float]) -> str:
    """A vector as pgvector reads it, because asyncpg has no codec for one."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


async def embed_rows(pool: asyncpg.Pool, embedder: Embedder, rows: list[Any]) -> int:
    """Embed these statements and store the vectors. Returns how many."""
    if not rows:
        return 0
    vectors = await asyncio.to_thread(embedder.documents, [row["statement"] for row in rows])
    stored = zip(rows, vectors, strict=True)
    await pool.executemany(STORE, [(row["id"], literal(v), embedder.model) for row, v in stored])
    return len(rows)


async def embed_message(
    pool: asyncpg.Pool, embedder: Embedder, session_id: str, entry_id: str
) -> int:
    """Embed the statements written from one message."""
    rows = await pool.fetch(UNEMBEDDED, embedder.model, session_id, entry_id, BATCH)
    return await embed_rows(pool, embedder, rows)


async def embed_missing(pool: asyncpg.Pool, embedder: Embedder, batch: int = BATCH) -> int:
    """Embed every live statement this model has not, in batches, until none is left."""
    total = 0
    while rows := await pool.fetch(UNEMBEDDED, embedder.model, None, None, batch):
        total += await embed_rows(pool, embedder, rows)
    return total


async def embed_statements(
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    embedder: Embedder = Shared(shared_embedder),
) -> None:
    """Embed whatever the model has not, which is how a model change is applied."""
    count = await embed_missing(pool, embedder)
    log.info("embedded %s statements with %s", count, embedder.model)
