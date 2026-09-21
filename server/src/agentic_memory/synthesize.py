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
from typing import Any

import asyncpg
import httpx
from docket import Depends, Shared

from .classify import KIND_COLUMNS, questions_fingerprint
from .db import store_pool
from .memories import retire, standing
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

# How sure the reading has to be that a message pushes back on something before
# the statements already held about the place are put in front of the writer.
# Measured over 376 readings, this question and the correction kind together
# qualify 35 of them, which is about one message in eleven. Everything else
# writes without any candidates, so the comparison against the table costs
# nothing on the messages that cannot replace anything.
CORRECTS_EARLIER = 0.70

# The reading for one message, with the state it judged and every score.
READING = f"""
    SELECT session_id, entry_id, scope_key, model, questions_fingerprint,
           state ->> 'before' AS before, state ->> 'message' AS message,
           (SELECT l.occurred_at FROM logs l
             WHERE l.session_id = classifications.session_id
               AND l.entry_id = classifications.entry_id) AS said_at,
           {", ".join(sorted(set(KIND_COLUMNS)))}
    FROM classifications
    WHERE session_id = $1 AND entry_id = $2 AND questions_fingerprint = $3
"""

# One statement per message per kind per question set, so a retry writes
# nothing and one message can carry a fact and a rule at once. The new id comes
# back because a statement that replaces another one names it.
RECORD = """
    INSERT INTO memories
        (statement, kind, score, scope_key, session_id, entry_id, model,
         questions_fingerprint, said_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (session_id, entry_id, kind, questions_fingerprint) DO NOTHING
    RETURNING id
"""

# The readings that have cleared a threshold and have no statements yet. The
# thresholds go in as values rather than as text, so the query is one statement
# that does not change when the numbers do.
#
# The order is oldest message first, so a message that pushes back is offered the
# statements already written from messages before it. Writing a history from
# newest to oldest would let a message from last year retire one from today.
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
    ORDER BY classified_at ASC
    LIMIT ${len(THRESHOLDS) + 2}
"""

SYSTEM = """You write down what a person wants their coding agents to remember.

You are given one message from a working session, the exchanges leading up to
it, and the kinds of memory that were found in that message. Write one sentence
for each kind that was found. Write nothing for a kind that was not listed.

A statement is what an agent should read before it starts work next week. Test
every sentence against this: would it still be worth reading if the work it
came from were finished and forgotten?

Rules:

- A task is not a memory. "Prototype the first level" describes work in flight
  and means nothing once it is done. "The first level is not started" describes
  the state of the work, and that is the form to write.

- Never write an instruction. The subject of the sentence is never the reader.
  Write "design discussion comes before implementation here", not "do not start
  implementing yet". An agent cannot tell a remembered rule from a rule it is
  being given now, so a memory written as an order will be followed at the
  wrong time.

- Name the subject. A pronoun that points outside its own sentence means
  nothing to a reader who was not there. Write the name of the thing.

- Write the condition, not the instruction. "Postgres may not be the end state,
  and a query taking more than a second is the trigger to move" tells a reader
  when the statement applies. "Continue with the plan" tells them nothing.

- Say which project it is about when it is not about the one the reader is in.
  "In equipment-operator, spec.zones must declare zone2 and zone3".

- Keep the specifics. A filename, a command, and a version are the parts worth
  remembering. "Prefers modern tooling" is worth nothing.

- One sentence. No preamble, and no explanation of your reasoning.
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
{extra}{standing}
Answer with a JSON array and nothing else. Each element has "kind" set to one
of the kinds above, "statement" set to one sentence, and "replaces" set to the
numbers of any statements the new one replaces.
An empty array means nothing here is worth remembering."""

EXTRA = """
Two more things were found, and they change how the sentences are written:
{notes}
"""

STANDING = """
The statements this place already holds, by number:

{standing}

If the message replaces any of them, name those numbers in "replaces" on the
statement doing the replacing. A statement replaces another when the two cannot
both be true, or when the new one settles a question the old one left open.
Adding a fact, an opinion, or a detail to one of them replaces nothing, and
naming a number ends the statement for good.
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


def pushes_back(reading: Any) -> bool:
    """Whether a message is worth offering the held statements to.

    Only a message that corrects something or pushes back on something older
    can replace a statement. Asking every message would put a list of the
    place's memories in front of the writer for no reason, and a writer shown
    the list will find something in it to replace.
    """
    return (
        reading["correction"] >= THRESHOLDS["correction"]
        or reading["corrects_earlier"] >= CORRECTS_EARLIER
    )


def offered_numbers(
    sentence: dict[str, Any],
    candidates: dict[int, dict[str, Any]],
) -> list[int]:
    """The statements a sentence says it replaces, by their ids.

    A model answers with numbers and with the numbers as text, so both are read.
    A number that was never offered is dropped, because the model can only end a
    statement that was put in front of it, and dropping it is logged so a
    replacement that did not happen is never silent.
    """
    named = sentence.get("replaces")
    if not isinstance(named, list):
        return []
    numbers = []
    for entry in named:
        try:
            number = int(entry)
        except TypeError, ValueError:
            log.warning("the writer named %r as a replacement, which is not a number", entry)
            continue
        if number not in candidates:
            log.warning("the writer named %s as a replacement, which was not offered", number)
            continue
        numbers.append(candidates[number]["id"])
    return numbers


def candidates_block(
    candidates: dict[int, dict[str, Any]],
) -> str:
    """The held statements as the writer reads them, or nothing when there are none."""
    if not candidates:
        return ""
    listed = "\n".join(
        f"[{number}] ({row['kind']}) {row['statement']}" for number, row in candidates.items()
    )
    return STANDING.format(standing=listed)


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

    # The statements this place holds, by the number the writer will name them
    # by. Nothing is offered unless the message pushes back on something.
    candidates: dict[int, dict[str, Any]] = {}
    if pushes_back(found):
        held = await standing(
            pool,
            scope_key=found["scope_key"],
            kinds=firing,
            said_before=found["said_at"],
        )
        candidates = dict(enumerate(held, start=1))

    instructions = INSTRUCTIONS.format(
        scope=found["scope_key"] or "unknown",
        before=found["before"] or "(nothing came before it)",
        message=found["message"] or "",
        kinds=", ".join(sorted(firing)),
        extra=EXTRA.format(notes="\n".join(f"- {note}" for note in notes)) if notes else "",
        standing=candidates_block(candidates),
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
        new_id = await pool.fetchval(
            RECORD,
            statement,
            kind,
            firing[kind],
            scope,
            session_id,
            entry_id,
            found["model"],
            found["questions_fingerprint"],
            found["said_at"],
        )
        if new_id is None:
            continue
        replaced = offered_numbers(sentence, candidates)
        if replaced:
            ended = await retire(pool, replaced=replaced, replacement=new_id)
            log.info("ended %s statements for %s %s: %s", len(ended), session_id, entry_id, ended)
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
