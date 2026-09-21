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
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from docket import Depends, Shared
from fastembed import TextEmbedding

from .db import store_pool
from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.embed")

BATCH = 256


class Embedder:
    """One loaded model, and the two ways to use it."""

    def __init__(self, model: str, threads: int, cache: str) -> None:
        self.model = model
        self._model = TextEmbedding(model_name=model, threads=threads, cache_dir=cache)

    def documents(self, texts: list[str]) -> list[list[float]]:
        """The vectors for statements, in the order given."""
        return [vector.tolist() for vector in self._model.embed(texts, batch_size=64)]

    def query(self, text: str) -> list[float]:
        """The vector for a prompt.

        The model prefixes a query and not a document, so a prompt and the
        statements it is compared with go through different calls.
        """
        return next(iter(self._model.query_embed(text))).tolist()


def load(settings: Settings) -> Embedder:
    return Embedder(settings.embed_model, settings.embed_threads, settings.embed_cache)


@asynccontextmanager
async def shared_embedder():
    """The model, loaded once and shared by every task on a worker."""
    yield await asyncio.to_thread(load, get_settings())


# The live statements this model has not embedded. A row embedded by another
# model counts as unembedded, because its vector is in another space.
UNEMBEDDED = """
    SELECT id, statement
    FROM memories
    WHERE superseded_by IS NULL
      AND (embedding IS NULL OR embedding_model IS DISTINCT FROM $1)
      AND ($2::text IS NULL OR (session_id = $2 AND entry_id = $3))
    ORDER BY id
    LIMIT $4
"""

STORE = """
    UPDATE memories
    SET embedding = $2::vector, embedding_model = $3
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
