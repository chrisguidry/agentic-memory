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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import asyncpg
from docket import CurrentDocket, Depends, Docket, ExponentialRetry, Shared
from typesafe_sdk import AsyncTypeSafeClient, Noul, TypeSafeBadRequestError

from .db import store_pool
from .settings import Settings, get_settings
from .window import NOT_PLUMBING, plumbing, window

log = logging.getLogger("agentic_memory.classify")

# What every question is told about the two halves of the state. The message is
# what is being judged. The exchanges before it are there to make the message
# readable, and judging them instead would score a window rather than a message.
ABOUT_THE_MESSAGE = {
    "inspect": "`message`",
    "focus": (
        "Judge the message itself. Use `before` only to work out what the message is replying to."
    ),
}

# One question per kind of memory the record can hold, and one per thing the
# kinds can be about. Each one asks about a single proposition, because the
# answer is the probability of that proposition and nothing else. A question
# about degree would come back as the probability of "yes" and would not measure
# the degree.
#
# A judgment that hides several questions inside it comes back as one number
# that cannot say which part was true, so the dimensions are asked apart and
# combined in code. Nothing here is asked that code can compute exactly, such as
# which speaker said it: the record already knows.
#
# The questions name the speakers rather than the person, because both sides
# state facts, intentions, and preferences and the record holds both. Which
# speaker said it is part of the memory, and trust ranks a person's statement
# above an agent's.
KINDS: dict[str, Noul] = {
    "semantic": Noul(
        instructions={
            "question": (
                "Does `message` state something about the code, the project, or how "
                "something works, that would still be true a month from now?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It states a fact about the work or the world.",
            "false": "It is only about this moment, or it states no fact.",
        },
    ),
    "procedural": Noul(
        instructions={
            "question": (
                "Does `message` say how something is done here, such as a command to "
                "run, a step in a process, or a way of working?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It describes a way of doing the work that would be followed again.",
            "false": "It describes this one run of the work, and not a way of doing it.",
        },
    ),
    "prospective": Noul(
        instructions={
            "question": (
                "Does `message` settle something, defer something, or leave a question "
                "open, in a way that later work has to respect?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It records a decision, a deferral, or an open question that outlives "
            "this session.",
            "false": "It only describes work to do, and settles nothing.",
        },
    ),
    "preference": Noul(
        instructions={
            "question": (
                "Does `message` state a rule or a taste that should hold in later "
                "conversations too?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It states a standing rule, a lasting taste, or a general dislike.",
            "false": "It picks an option for this task only, or states no preference.",
        },
    ),
    "correction": Noul(
        instructions={
            "question": (
                "Does `message` correct the other speaker, reject what the other did, "
                "or tell the other to do something differently?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "The speaker of the message pushes back on the other's work.",
            "false": "The speaker accepts the work, or asks for something new.",
        },
    ),
    "praise": Noul(
        instructions={
            "question": (
                "Does `message` approve of the other speaker's work or its approach, "
                "rather than only acknowledging that a task finished?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It says the work or the approach was right, or is what was wanted.",
            "false": "It only confirms a task finished, or says nothing about the work.",
        },
    ),
    # The kinds above say what is there. These say what it is about, and they are
    # asked alongside rather than only when their kind is present, because a
    # question costs tokens and almost no time and code decides what is relevant.
    "corrects_earlier": Noul(
        instructions={
            "question": (
                "Does `message` correct a belief or a decision that was already in place "
                "before this conversation, rather than something that just happened in it?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "The thing being corrected predates this conversation.",
            "false": "The thing being corrected happened in this conversation, or nothing "
            "is being corrected.",
        },
    ),
    "praise_outcome": Noul(
        instructions={
            "question": (
                "Does `message` approve of the state of things, rather than of a "
                "particular action someone took?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It approves of the result or the current state.",
            "false": "It approves of a specific action, or gives no approval.",
        },
    ),
    "about_artifact": Noul(
        instructions={
            "question": (
                "Does `message` name a particular file, command, or tool that the "
                "statement is about?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It names a file, a command, or a tool.",
            "false": "It is about the work in general, and names no particular thing.",
        },
    ),
    "beyond_this_project": Noul(
        instructions={
            "question": (
                "Does `message` state something that would hold in other projects too, "
                "rather than only in this one?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It states a rule or a fact that is not particular to this project.",
            "false": "It is only true of this project, or it states nothing about how to work.",
        },
    ),
    "forbids": Noul(
        instructions={
            "question": (
                "Does `message` say not to do something, or name something that should be avoided?"
            ),
            **ABOUT_THE_MESSAGE,
        },
        criteria={
            "true": "It rules something out, or names something to stay away from.",
            "false": "It asks for something to be done, or rules nothing out.",
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

# Messages that arrived in a range, for reading a part of the history again.
# The range is on when the message happened rather than when it was stored, so
# a backfill of an old session lands in the range it belongs to.
READABLE = f"""
    SELECT session_id, entry_id, body
    FROM logs
    WHERE kind = 'prompt'
      AND occurred_at >= $1 AND occurred_at < $2
      AND entry_id IS NOT NULL
      AND ($3::text IS NULL OR harness = $3)
      AND {NOT_PLUMBING}
    ORDER BY occurred_at DESC
    LIMIT $4
"""

# The kinds in a fixed order, so the columns a reading writes and the questions
# it asks cannot drift apart.
KIND_COLUMNS = tuple(KINDS)
KIND_COLUMNS_VALUES = ", ".join(f"${n}" for n in range(10, 10 + len(KIND_COLUMNS)))

# One reading per window per model per question set, so a retry writes nothing,
# a second model can be added beside the first, and changing a question does not
# leave the old answers standing as though they were answers to the new one.
RECORD = f"""
    INSERT INTO classifications
        (session_id, entry_id, scope_key, model, questions_fingerprint, rounds,
         actor, actor_depth, state, {", ".join(KIND_COLUMNS)})
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, {KIND_COLUMNS_VALUES})
    ON CONFLICT (session_id, entry_id, model, questions_fingerprint) DO NOTHING
"""


# A provider that is down or rate-limited answers with an error, and without a
# retry the message is never read: the task is keyed by the entry, so nothing
# schedules it again until someone re-reads the range by hand. A few attempts
# spread over a minute cover an outage of the length seen so far. A reply the
# code cannot use is not raised, so it is not retried; that is a defect to log
# and read again under a fixed question, not a transient to wait out.
MODEL_RETRY = ExponentialRetry(
    attempts=4, minimum_delay=timedelta(seconds=2), maximum_delay=timedelta(seconds=30)
)


def worth_reading(prompts: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The prompts to read, out of the prompts that were written.

    A harness writes a third of its own plumbing as though the person had typed
    it. Those are dropped here rather than behind the queue, so a scheduled task
    is always a task with something to read.
    """
    return [(session_id, entry_id) for session_id, entry_id, body in prompts if not plumbing(body)]


def task_key(session_id: str, entry_id: str) -> str:
    """The name of one scheduled reading.

    The question set is part of the name, so reading the same message again
    under new questions is its own piece of work rather than a message that was
    already handled.
    """
    return f"classify:{session_id}:{entry_id}:{KINDS_FINGERPRINT}"


def statement_key(session_id: str, entry_id: str) -> str:
    """The name of one scheduled piece of writing, keyed the same way."""
    return f"synthesize:{session_id}:{entry_id}:{KINDS_FINGERPRINT}"


async def readable_prompts(
    pool: asyncpg.Pool,
    *,
    since: datetime,
    until: datetime,
    harness: str | None = None,
    limit: int = 500,
) -> list[tuple[str, str]]:
    """The messages worth reading that happened in a range.

    This is how the questions get calibrated. Change one, read the last few days
    again, and compare the answers, without reading the whole history and
    without disturbing the readings taken under the questions before it.
    """
    found = await pool.fetch(READABLE, since, until, harness, limit)
    return worth_reading([(row["session_id"], row["entry_id"], row["body"]) for row in found])


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
    docket: Docket = CurrentDocket(),
    retry: ExponentialRetry = MODEL_RETRY,
) -> None:
    """Read one message and write down what kinds of memory are in it."""
    found = await window(pool, session_id, entry_id, settings.classify_rounds)
    if found is None or not found.message:
        return

    state = found.fitted(settings.classify_budget).state()
    try:
        response = await client.system_one(state=state, questions=KINDS)
    except TypeSafeBadRequestError:
        # The provider refused the request itself, so sending it again gets the
        # same refusal. This is a window the fit did not make small enough, or
        # a question the provider will not take, and either is a defect to fix
        # and then read again, not a transient to wait out.
        log.warning("%s %s: the model refused the request", session_id, entry_id, exc_info=True)
        return
    verdicts = {kind: answer.noul for kind, answer in response.nouls.items()}

    # A partial answer is a defect rather than data, because a reading missing a
    # kind cannot be compared with one that has it. The task is idempotent, so a
    # later pass can read this message again.
    if set(verdicts) != set(KINDS):
        log.warning(
            "%s %s: answered %s of %s kinds", session_id, entry_id, len(verdicts), len(KINDS)
        )
        return

    await pool.execute(
        RECORD,
        session_id,
        entry_id,
        found.scope_key,
        # The versioned model, not the alias that was asked for, because the
        # alias moves and the answers move with it.
        response.model,
        KINDS_FINGERPRINT,
        settings.classify_rounds,
        found.actor,
        found.actor_depth,
        json.dumps(state),
        *(verdicts[kind] for kind in KIND_COLUMNS),
    )
    log.info("read %s %s: %s", session_id, entry_id, verdicts)

    # A message that cleared no threshold never reaches the larger model, which
    # is where the cost is. The import is here because the writer reads this
    # module for the question set, and a module-level import would be a cycle.
    from .synthesize import THRESHOLDS, synthesize

    if any(verdicts[kind] >= THRESHOLDS[kind] for kind in THRESHOLDS):
        await docket.add(synthesize, key=statement_key(session_id, entry_id))(session_id, entry_id)
