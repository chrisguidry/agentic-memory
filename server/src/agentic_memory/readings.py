"""The prompts the classifier read, embedded, so a new prompt can be compared with them.

The classifier answers each kind's question about every prompt a person types.
A prompt whose answers all fell below their thresholds held no memory, and most
of those are replies with no subject of their own, such as "keep going" or
"looks good". The match scores a reply like that well against a statement in
the same register, so a new prompt that lands among them is handed nothing
rather than matched.

The worker embeds the prompt of each reading after the reading is written, with
the local model the turn path embeds a prompt with, and the same call, so the
two vectors are comparable. The turn path then reads the nearest readings in one
indexed query and calls no model over a network. The set it compares with grows
as the classifier reads, so it follows the way the person writes without anyone
listing phrases.
"""

import asyncio
import logging

import asyncpg
from docket import Shared

from .db import store_pool
from .embed import Embedder, literal, shared_embedder
from .metrics import phase
from .settings import Settings
from .synthesize import THRESHOLDS

log = logging.getLogger("agentic_memory.readings")

BATCH = 256

# Whether a reading held no memory: every kind below the threshold the writer
# uses, so "no memory" here means what it means to the writer. The thresholds go
# in as values, so the query does not change when the numbers do.
EMPTY = " AND ".join(f"{kind} < ${number}" for number, kind in enumerate(THRESHOLDS, start=5))

# The readings nearest a prompt, whatever question set they were read under. An
# answer to an older question set still says whether that prompt held a memory,
# and without the older readings the comparison would start from nothing each
# time the questions change.
#
# A reading taken after the moment is left out, so a replay reads the readings
# as they stood when the prompt was said, and a prompt is never its own
# neighbour.
NEIGHBOURS = f"""
    SELECT {EMPTY} AS empty
    FROM classifications
    WHERE prompt_embedding IS NOT NULL
      AND prompt_embedding_model = $2
      AND ($3::timestamptz IS NULL OR classified_at <= $3)
    ORDER BY prompt_embedding <=> $1::vector
    LIMIT $4
"""


def short(prompt: str, words: int) -> bool:
    """Whether a prompt has few enough words to be judged by its neighbours.

    Most prompts that ask for work hold no memory either, so a long request
    lands among readings that held none, and its neighbours would silence it.
    A reply with no subject of its own is short, so only a short prompt is
    judged.
    """
    return len(prompt.split()) <= words


async def holds_nothing(
    pool: asyncpg.Pool,
    vector: list[float],
    *,
    model: str,
    settings: Settings,
    as_of=None,
) -> bool:
    """Whether a prompt's nearest readings mostly held no memory.

    `vector` is the prompt embedded by `model`. Fewer readings than the
    neighbourhood decide nothing, because a share of a handful of readings is
    not a measure, and the prompt is matched as usual.
    """
    with phase("neighbours"):
        found = await pool.fetch(
            NEIGHBOURS,
            literal(vector),
            model,
            as_of,
            settings.recall_neighbours,
            *THRESHOLDS.values(),
        )
    if len(found) < settings.recall_neighbours:
        return False
    empty = sum(row["empty"] for row in found)
    return empty / len(found) > settings.recall_empty_share


# The readings this model has not embedded, of one prompt or of every prompt. A
# reading embedded by another model counts as unembedded, because its vector is
# in another space. A reading with no message has nothing to embed, and leaving
# it out keeps the backfill from reading it again on every batch.
UNEMBEDDED = """
    SELECT id, state ->> 'message' AS message
    FROM classifications
    WHERE (prompt_embedding IS NULL OR prompt_embedding_model IS DISTINCT FROM $1)
      AND nullif(btrim(state ->> 'message'), '') IS NOT NULL
      AND ($2::text IS NULL OR (session_id = $2 AND entry_id = $3))
    ORDER BY id
    LIMIT $4
"""

STORE = """
    UPDATE classifications
    SET prompt_embedding = $2::vector, prompt_embedding_model = $3
    WHERE id = $1
"""


def reading_key(session_id: str, entry_id: str) -> str:
    """The name of one scheduled embedding of a reading's prompt."""
    return f"embed-reading:{session_id}:{entry_id}"


async def embed_rows(pool: asyncpg.Pool, embedder: Embedder, rows: list) -> int:
    """Embed the prompts of these readings and store the vectors. Returns how many."""
    if not rows:
        return 0
    vectors = await asyncio.to_thread(embedder.queries, [row["message"] for row in rows])
    stored = zip(rows, vectors, strict=True)
    await pool.executemany(STORE, [(row["id"], literal(v), embedder.model) for row, v in stored])
    return len(rows)


async def embed_readings(pool: asyncpg.Pool, embedder: Embedder, batch: int = BATCH) -> int:
    """Embed the prompt of every reading this model has not, until none is left."""
    total = 0
    while rows := await pool.fetch(UNEMBEDDED, embedder.model, None, None, batch):
        total += await embed_rows(pool, embedder, rows)
    return total


async def embed_reading(
    session_id: str,
    entry_id: str,
    *,
    pool: asyncpg.Pool = Shared(store_pool),
    embedder: Embedder = Shared(shared_embedder),
) -> None:
    """Embed the prompt of one reading, as a task that follows the reading."""
    rows = await pool.fetch(UNEMBEDDED, embedder.model, session_id, entry_id, BATCH)
    await embed_rows(pool, embedder, rows)
