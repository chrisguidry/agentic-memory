"""Turning a message a person wrote into a sentence worth reading again.

The classifier says what is in a message. This says it in one sentence. It
runs on the messages the classifier scored highly and writes nothing for the
rest, so the larger model sees a fraction of the record.

A statement is written once. The task is keyed by the message and the question
set, so a retry writes nothing and a re-read under new questions writes beside
the old statements instead of over them.
"""

import json
import logging
from contextlib import asynccontextmanager

import asyncpg
import httpx
from docket import Depends, Shared

from .classify import KIND_COLUMNS, questions_fingerprint
from .db import store_pool
from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.synthesize")

COMPLETIONS = "https://api.deepinfra.com/v1/openai/chat/completions"

# How sure a reading has to be before it becomes a sentence. One number per
# kind, because the kinds do not fire at the same rate: praise and preference
# run high and correction runs low, so one number would take nearly everything
# from one kind and nearly nothing from another.
#
# These are the dial for how full the store gets. They are set by reading what
# comes out, not by a formula.
THRESHOLDS: dict[str, float] = {
    "semantic": 0.80,
    "procedural": 0.80,
    "prospective": 0.85,
    "preference": 0.85,
    "correction": 0.70,
    "praise": 0.80,
}

# The reading for one message, with the state it judged and every score.
READING = f"""
    SELECT session_id, entry_id, scope_key, model, questions_fingerprint,
           state ->> 'before' AS before, state ->> 'message' AS message,
           {", ".join(sorted(set(KIND_COLUMNS)))}
    FROM classifications
    WHERE session_id = $1 AND entry_id = $2 AND questions_fingerprint = $3
"""

# One statement per message per kind per question set, so a retry writes
# nothing and one message can carry a fact and a rule at once.
RECORD = """
    INSERT INTO memories
        (statement, kind, score, scope_key, session_id, entry_id, model,
         questions_fingerprint)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (session_id, entry_id, kind, questions_fingerprint) DO NOTHING
"""

# The readings that have cleared a threshold and have no statements yet. The
# thresholds go in as values rather than as text, so the query is one statement
# that does not change when the numbers do.
WORTH_WRITING = f"""
    SELECT session_id, entry_id
    FROM classifications
    WHERE questions_fingerprint = $1
      AND ({" OR ".join(f"{kind} >= ${number}" for number, kind in enumerate(THRESHOLDS, start=2))})
      AND NOT EXISTS (
          SELECT 1 FROM memories m
          WHERE m.session_id = classifications.session_id
            AND m.entry_id = classifications.entry_id
            AND m.questions_fingerprint = classifications.questions_fingerprint
      )
    ORDER BY classified_at DESC
    LIMIT ${len(THRESHOLDS) + 2}
"""

SYSTEM = """You write down what a person wants their coding agents to remember.

You are given one message from a working session, the exchanges leading up to
it, and the kinds of memory that were found in that message. Write one sentence
for each kind that was found. Write nothing for a kind that was not listed.

A sentence is what the person would want to read when they start work next
week. Write it in their vocabulary, using the words they used.

Rules:
- State what is true, not what happened. "The schema is at server/schema.sql"
  rather than "the person asked about the schema".
- Keep the specifics. A filename, a command, and a version are the parts worth
  remembering. "Prefers modern tooling" is worth nothing.
- Write a rule as a rule. If the person does not want something done, say so.
- If it applies to every project rather than this one, say so.
- If the message carries nothing an agent could act on, write nothing.
"""

INSTRUCTIONS = """Scope: {scope}

The exchanges leading up to the message, oldest first:

<before>
{before}
</before>

The message itself, which is what you are writing about:

<message>
{message}
</message>

The kinds of memory found in that message: {kinds}
{extra}
Answer with a JSON array and nothing else. Each element has "kind" set to one
of the kinds above and "statement" set to one sentence. An empty array means
nothing here is worth remembering."""

EXTRA = """
Two more things were found, and they change how the sentences are written:
{notes}
"""


@asynccontextmanager
async def completions_client():
    """The model client, opened once and shared by every task on a worker."""
    async with httpx.AsyncClient(timeout=90) as client:
        yield client


def writing_from(kind: str) -> bool:
    """Whether a kind is one the model should write a sentence for.

    The questions about what a statement is about steer the writing rather than
    producing statements of their own, so a model that returns one of those is
    ignored.
    """
    return kind in THRESHOLDS


def parse(reply: str) -> list[dict]:
    """The statements in a reply, or nothing when the reply is not a list."""
    opened = reply.find("[")
    closed = reply.rfind("]")
    if opened == -1 or closed <= opened:
        log.warning("no array in the reply: %s", reply[:200])
        return []
    try:
        found = json.loads(reply[opened : closed + 1])
    except json.JSONDecodeError:
        log.warning("could not read the reply: %s", reply[:200])
        return []
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


async def ask(client: httpx.AsyncClient, settings: Settings, instructions: str) -> str:
    """One completion from the model that writes the sentences."""
    response = await client.post(
        COMPLETIONS,
        headers={"Authorization": f"Bearer {settings.deepinfra_api_key}"},
        json={
            "model": settings.synthesize_model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": instructions},
            ],
            "max_tokens": 700,
            "temperature": 0,
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


async def worth_writing(
    pool: asyncpg.Pool,
    limit: int = 500,
) -> list[tuple[str, str]]:
    """The readings that cleared a threshold and have no statements yet."""
    found = await pool.fetch(WORTH_WRITING, questions_fingerprint(), *THRESHOLDS.values(), limit)
    return [(row["session_id"], row["entry_id"]) for row in found]


async def write(
    session_id: str,
    entry_id: str,
    *,
    settings: Settings,
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
) -> list[str]:
    """Read one message and write the sentences the model returns."""
    found = await pool.fetchrow(READING, session_id, entry_id, questions_fingerprint())
    if found is None:
        return []

    # Only the kinds that cleared their own threshold are worth a sentence.
    firing = {kind: found[kind] for kind in THRESHOLDS if found[kind] >= THRESHOLDS[kind]}
    if not firing:
        return []

    notes = []
    if found["beyond_this_project"] >= 0.7:
        notes.append("It applies beyond this project, so write it as a general rule.")
    if found["forbids"] >= 0.7:
        notes.append("It rules something out, so write it as a thing not to do.")

    instructions = INSTRUCTIONS.format(
        scope=found["scope_key"] or "unknown",
        before=found["before"] or "(nothing came before it)",
        message=found["message"] or "",
        kinds=", ".join(sorted(firing)),
        extra=EXTRA.format(notes="\n".join(f"- {note}" for note in notes)) if notes else "",
    )

    sentences = parse(await ask(client, settings, instructions))
    if not sentences:
        return []

    # A statement that holds everywhere has no scope of its own, and a null
    # scope is reachable from every scope.
    scope = None if found["beyond_this_project"] >= 0.7 else found["scope_key"]

    written = []
    for sentence in sentences:
        kind = sentence.get("kind")
        statement = (sentence.get("statement") or "").strip()
        if not writing_from(kind) or kind not in firing or not statement:
            continue
        await pool.execute(
            RECORD,
            statement,
            kind,
            firing[kind],
            scope,
            session_id,
            entry_id,
            found["model"],
            found["questions_fingerprint"],
        )
        written.append(statement)

    return written


async def synthesize(
    session_id: str,
    entry_id: str,
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    client: httpx.AsyncClient = Shared(completions_client),
) -> None:
    """Turn one classified message into statements, if it is worth any."""
    written = await write(session_id, entry_id, settings=settings, pool=pool, client=client)
    if written:
        log.info("wrote %s for %s %s: %s", len(written), session_id, entry_id, written)
