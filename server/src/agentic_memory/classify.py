"""Reading a window of a session and scoring what kinds of memory are in it.

Nothing here writes a memory. The task reads a window, asks a System One model
one yes/no question per kind of memory, and writes down the probabilities.

The reading stores a probability, not a decision about it, so the threshold
belongs to whatever reads this table. Moving the threshold costs a query rather
than another pass over the record.

The provider documents that the answers are independent, so one question's
answer is not context for another's and a kind can be added or removed without
changing the rest.
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
from docket import Depends, Shared
from typesafe_sdk import AsyncTypeSafeClient, Noul

from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.classify")

# One question per kind of memory the record can hold. Each one asks about a
# single proposition, because the answer is the probability of that proposition
# and nothing else. A question about degree would come back as the probability
# of "yes" and would not measure the degree.
#
# The questions name the speakers rather than the person, because both sides
# state facts, intentions, and preferences and the record holds both. Which
# speaker said it is part of the memory, and trust ranks a person's statement
# above an agent's.
#
# Episodic memory is missing on purpose: the session is the record, so there is
# nothing to extract. Sensory memory has no channel in text. Working memory is
# the session in progress.
KINDS: dict[str, Noul] = {
    "semantic": Noul(
        instructions=(
            "Does this transcript state something about the code, the project, or how "
            "something works, that would still be true a month from now?"
        ),
        criteria={
            "true": "It states a fact about the work or the world.",
            "false": "It is only about this moment, or it states no fact.",
        },
    ),
    "procedural": Noul(
        instructions=(
            "Does this transcript say how something is done here, such as a command to "
            "run, a step in a process, or a way of working?"
        ),
        criteria={
            "true": "It describes a way of doing the work that would be followed again.",
            "false": "It describes only this one instance of the work.",
        },
    ),
    "prospective": Noul(
        instructions=(
            "Does this transcript state something one of the speakers means to do later, "
            "or leave something unfinished that they will come back to?"
        ),
        criteria={
            "true": "It names an intention, a follow-up, or work left open.",
            "false": "It names nothing left to do.",
        },
    ),
    "preference": Noul(
        instructions=(
            "Does this transcript state how one of the speakers wants things done, or "
            "name something they dislike?"
        ),
        criteria={
            "true": "It states a taste, a standing preference, or a dislike.",
            "false": "It states no preference.",
        },
    ),
    "correction": Noul(
        instructions=(
            "Does this transcript show one speaker correcting the other, rejecting what "
            "the other did, or telling the other to do something differently?"
        ),
        criteria={
            "true": "One speaker pushes back on the other's work or choice.",
            "false": "Neither pushes back: the work is accepted, or a new request is made.",
        },
    ),
    "praise": Noul(
        instructions=(
            "Does this transcript show one speaker approving of the other's work or its "
            "approach, rather than only acknowledging that a task finished?"
        ),
        criteria={
            "true": "One speaker says the work or the approach was right, or that it is "
            "what they wanted.",
            "false": "One speaker only confirms a task finished, or says nothing about the work.",
        },
    ),
}


def questions_fingerprint() -> str:
    """A short name for the question set, so a reading records what it was asked.

    The model name cannot do this job, because the questions move without the
    model moving, and an answer to the old question is not an answer to the new
    one.
    """
    asked = {kind: question.model_dump() for kind, question in KINDS.items()}
    return hashlib.sha256(json.dumps(asked, sort_keys=True).encode()).hexdigest()[:16]


KINDS_FINGERPRINT = questions_fingerprint()

# Entries a harness writes for itself rather than from the conversation. Claude
# Code and pi both record injected skill text, command wrappers, and interrupt
# markers as though the person had typed them, which is a third of everything
# stored as a prompt and none of what the person wanted.
NOT_PLUMBING = """
    btrim(body) NOT LIKE '<%'
    AND btrim(body) NOT LIKE '[Request interrupted%'
    AND btrim(body) NOT LIKE 'Base directory for this skill%'
"""

# The prompt being read, and where it happened. A prompt with no entry id of
# its own cannot be pointed at again, and a plumbing entry is not the person.
TARGET = f"""
    SELECT occurred_at, scope_key
    FROM logs
    WHERE session_id = $1 AND entry_id = $2 AND kind = 'prompt'
      AND {NOT_PLUMBING}
    LIMIT 1
"""

RECENT_PROMPTS = f"""
    SELECT occurred_at
    FROM logs
    WHERE session_id = $1 AND kind = 'prompt'
      AND occurred_at <= $2
      AND {NOT_PLUMBING}
    ORDER BY occurred_at DESC
    LIMIT $3
"""

SPAN = """
    SELECT occurred_at, kind, body
    FROM logs
    WHERE session_id = $1 AND kind IN ('prompt', 'response')
      AND occurred_at >= $2 AND occurred_at <= $3
    ORDER BY occurred_at
"""

# One reading per window per model per question set, so a retry writes nothing,
# a second model can be added beside the first, and changing a question does not
# leave the old answers standing as though they were answers to the new one.
RECORD = """
    INSERT INTO classifications
        (session_id, entry_id, scope_key, model, questions_fingerprint, rounds,
         transcript, verdicts, best)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
    ON CONFLICT (session_id, entry_id, model, questions_fingerprint) DO NOTHING
"""


@dataclass(frozen=True)
class Window:
    """One prompt, the exchanges leading to it, and where it happened."""

    transcript: str
    scope_key: str | None


def plumbing(body: str) -> bool:
    """Whether a harness wrote this entry for itself rather than the person."""
    return body.lstrip().startswith(("<", "[Request interrupted", "Base directory for this skill"))


def spoken(rows: Sequence[Any]) -> str:
    """The conversation in a span, as the two sides of it.

    An agent turn arrives as many records and most of them are empty, so the
    records that hold no text are dropped rather than rendered as blank turns.
    """
    lines = []
    for found in rows:
        body = (found["body"] or "").strip()
        if not body or (found["kind"] == "prompt" and plumbing(body)):
            continue
        lines.append(f"[{'person' if found['kind'] == 'prompt' else 'agent'}] {body}")
    return "\n\n".join(lines)


async def window(
    pool: asyncpg.Pool,
    session_id: str,
    entry_id: str,
    rounds: int,
) -> Window | None:
    """The exchanges leading to one prompt, as text for the model to read.

    The window ends at the prompt being read, because the answer to that
    prompt does not exist yet. None comes back when the prompt is not in the
    record or the harness wrote the entry for itself, and both mean there is
    nothing to read.
    """
    target = await pool.fetchrow(TARGET, session_id, entry_id)
    if target is None:
        return None

    recent = await pool.fetch(RECENT_PROMPTS, session_id, target["occurred_at"], rounds)
    if not recent:
        return None

    opened = min(found["occurred_at"] for found in recent)
    span = await pool.fetch(SPAN, session_id, opened, target["occurred_at"])
    return Window(spoken(span), target["scope_key"])


@asynccontextmanager
async def store_pool():
    """The store, opened once and shared by every task on a worker."""
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def model_client():
    """The System One client, opened once and shared by every task on a worker."""
    settings = get_settings()
    async with AsyncTypeSafeClient(
        api_key=settings.typesafe_api_key or None,
        model=settings.classify_model,
    ) as client:
        yield client


async def classify(
    session_id: str,
    entry_id: str,
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    client: AsyncTypeSafeClient = Shared(model_client),
) -> None:
    """Read one window and write down what kinds of memory are in it."""
    found = await window(pool, session_id, entry_id, settings.classify_rounds)
    if found is None or not found.transcript:
        return

    response = await client.system_one(
        state={"transcript": found.transcript},
        questions=KINDS,
    )
    verdicts = {kind: answer.noul for kind, answer in response.nouls.items()}
    if not verdicts:
        log.warning("no answers for %s %s", session_id, entry_id)
        return

    await pool.execute(
        RECORD,
        session_id,
        entry_id,
        found.scope_key,
        settings.classify_model,
        KINDS_FINGERPRINT,
        settings.classify_rounds,
        found.transcript,
        json.dumps(verdicts),
        max(verdicts.values()),
    )
    log.info("read %s %s: %s", session_id, entry_id, verdicts)
