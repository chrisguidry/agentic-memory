"""Sorting a prospective statement into a decision, the state of the work, or a commitment.

The classifier's prospective question catches three things, and the writer
writes all three as prospective. A decision holds until someone changes it,
which is how a semantic or procedural statement behaves, and the ranking fades
a prospective statement out in weeks. The state of the work is true for an
hour, and the next session reads it from git. Only a commitment belongs in the
kind, and a commitment needs the condition that ends it.

So the System One model answers three yes/no questions about each
prospective statement and the exchange it came from: is it a commitment, is it
a decision, and is it only the state of the work. It also chooses which kind a
decision is and when a commitment ends. The code then acts on the answers:

- a decision is written again as semantic or procedural, and the prospective
  statement retires into it
- the state of the work ends with nothing to replace it
- a commitment keeps its kind and takes a moment or an event as its condition

The model answers each question on its own, so the sort does not depend on the
writer following a format. The questions are asked off the turn path: by the
worker after the writer writes, and by a backfill for the statements written
before the sort existed.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import asyncpg
from typesafe_sdk import Choice, Noul, TypeSafeBadRequestError

from .ledger import RecordedSystemOne, calling
from .memories import end, live, retire
from .settings import Settings

log = logging.getLogger("agentic_memory.sorting")

# The task the ledger records these calls under.
TASK = "sort"

# What every question is told about the state. The statement is what is judged.
# The message and the exchanges before it say what the statement meant when it
# was written, and `said` is the moment a "tomorrow" or a "Monday" counts from.
ABOUT_THE_STATEMENT = {
    "inspect": "`statement`",
    "focus": (
        "Judge the statement. Use `message` and `before` only to work out what the "
        "statement meant when it was said, and `said` for when it was said."
    ),
}

QUESTIONS: dict[str, Noul | Choice] = {
    "decision": Noul(
        instructions={
            "question": (
                "Does `statement` record a choice about how something is or will be built, "
                "a fact about how something works, a plan, or an open design question, "
                "which stays true after the current work is finished and until someone "
                "decides otherwise?"
            ),
            **ABOUT_THE_STATEMENT,
        },
        criteria={
            "true": (
                "It holds after this work is done, until someone changes it or settles it. "
                "A choice that is made and not built yet, and a question that is still "
                "open, both hold."
            ),
            "false": (
                "It names its own end, such as a moment or an event, or it is only about "
                "the work in progress now."
            ),
        },
    ),
    "commitment": Noul(
        instructions={
            "question": (
                "Does `statement` hold something back, or hold to something, until a named "
                "moment or event, such as not releasing before Monday, or waiting for a "
                "reply before opening a pull request?"
            ),
            **ABOUT_THE_STATEMENT,
        },
        criteria={
            "true": "It names what ends it, and later work has to respect it until then.",
            "false": "It names nothing that ends it.",
        },
    ),
    "state": Noul(
        instructions={
            "question": (
                "Is `statement` only about the work in progress now, such as files not yet "
                "committed or pushed, a build or a review in progress, something found "
                "while debugging, a task that was asked for, or a permission given for "
                "this one piece of work?"
            ),
            **ABOUT_THE_STATEMENT,
        },
        criteria={
            "true": "It stops being true, or stops mattering, once the current work is finished.",
            "false": "It still matters to a session that starts after this work is finished.",
        },
    ),
    "kind": Choice(
        instructions={
            "question": "Is `statement` about what something is, or about how the work is done?",
            **ABOUT_THE_STATEMENT,
        },
        criteria={
            "semantic": (
                "A fact about the code, the project, or how something works, including a "
                "choice about the design."
            ),
            "procedural": (
                "How something is done here: a command, a step, an order of steps, or a "
                "way of working."
            ),
        },
    ),
    "ends": Choice(
        instructions={
            "question": "When does `statement` stop holding?",
            **ABOUT_THE_STATEMENT,
        },
        criteria={
            "today": "At the end of the day `said` names.",
            "tomorrow_morning": "On the morning of the day after the one `said` names.",
            "tomorrow": "At the end of the day after the one `said` names.",
            "monday": "On the first Monday morning after the day `said` names.",
            "event": (
                "When something happens that a later message would report, such as a "
                "review passing, a reply arriving, or a release going out."
            ),
            "none": "It names no moment and no event.",
        },
    ),
}

# The hour a morning starts, in the person's zone. "Tomorrow morning" and
# "Monday" mean the start of the working day, and midnight would end a
# commitment while the person is asleep and still holding to it.
MORNING = time(9)

# The live prospective statements the sort has not answered, of one message or of
# every message, oldest first. The message and the exchanges before it come from
# the reading the statement was written from.
UNSORTED = f"""
    SELECT m.id, m.statement, m.scope_key, m.session_id, m.entry_id, m.said_at,
           m.questions_fingerprint, c.state ->> 'before' AS before,
           c.state ->> 'message' AS message
    FROM memories m
    LEFT JOIN LATERAL (
        SELECT state FROM classifications c
        WHERE c.session_id = m.session_id
          AND c.entry_id = m.entry_id
          AND c.questions_fingerprint = m.questions_fingerprint
        ORDER BY c.classified_at DESC
        LIMIT 1
    ) c ON true
    WHERE m.kind = 'prospective'
      AND {live("m")}
      AND m.sorted_at IS NULL
      AND ($1::text IS NULL OR (m.session_id = $1 AND m.entry_id = $2))
    ORDER BY m.id
"""

# Claims a statement for the sort, in the transaction that writes where it goes.
# A backfill lists its statements before it asks about them, and the worker or a
# merge can retire or sort one in the meantime. A statement that is no longer
# live and unsorted comes back empty here and is left as it is, so the sort
# never brings back a retired statement or writes a second answer over a first.
CLAIM = f"""
    UPDATE memories SET sorted_at = now()
    WHERE id = $1 AND kind = 'prospective' AND sorted_at IS NULL AND {live()}
    RETURNING id
"""

# A decision written again under its new kind. It keeps everything the writer
# and the worker already worked out for it, so it is matched and ranked at once.
# It runs after the claim, in the same transaction, so its source is a live
# statement that no one else is sorting. One statement per message per kind is
# the rule the table enforces, so a message that already wrote a statement of
# this kind returns nothing here.
REKIND = f"""
    INSERT INTO memories
        (statement, kind, score, scope_key, session_id, entry_id, model,
         questions_fingerprint, said_at, actor, actor_depth, embedding,
         embedding_model, actionable)
    SELECT statement, $2, score, scope_key, session_id, entry_id, model,
           questions_fingerprint, said_at, actor, actor_depth, embedding,
           embedding_model, actionable
    FROM memories
    WHERE id = $1 AND {live()}
    ON CONFLICT (session_id, entry_id, kind, questions_fingerprint) DO NOTHING
    RETURNING id
"""

CONDITION = "UPDATE memories SET until_moment = $2, until_event = $3 WHERE id = $1"


@dataclass(frozen=True)
class Answers:
    """What the model said about one statement."""

    decision: float
    commitment: float
    state: float
    kind: str
    ends: str
    ends_confidence: float


@dataclass(frozen=True)
class Placement:
    """Where one statement goes, and the condition a commitment takes."""

    outcome: Literal["decision", "state", "commitment"]
    kind: str | None = None
    until_moment: datetime | None = None
    until_event: str | None = None


def moments(said_at: datetime, zone: ZoneInfo) -> dict[str, datetime]:
    """The moments a statement said at `said_at` can end at, in the person's zone.

    Each moment is built as a wall-clock time in the zone, so a morning across a
    change to or from summer time is still nine o'clock.
    """
    day = said_at.astimezone(zone).date()
    after = day + timedelta(days=1)
    monday = day + timedelta(days=7 - day.weekday())

    def at(date, hour: time) -> datetime:
        return datetime.combine(date, hour, tzinfo=zone)

    return {
        "today": at(after, time(0)),
        "tomorrow_morning": at(after, MORNING),
        "tomorrow": at(after + timedelta(days=1), time(0)),
        "monday": at(monday, MORNING),
    }


def said_on(said_at: datetime | None, zone: ZoneInfo) -> str:
    """When a statement was said, in the person's zone, with the day of the week."""
    if said_at is None:
        return "an unknown moment"
    return said_at.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z, a %A")


async def ask(
    client: RecordedSystemOne, row: asyncpg.Record, settings: Settings, run: str
) -> Answers | None:
    """The model's answers about one statement, or none when it refused.

    The ledger records the refusal. Any other failure is raised, so the worker
    tries the task again.
    """
    state = {
        "statement": row["statement"],
        "said": said_on(row["said_at"], settings.time_zone),
        "before": row["before"] or "",
        "message": row["message"] or "",
    }
    try:
        with calling(TASK, session_id=row["session_id"], entry_id=row["entry_id"], run=run):
            response = await client.system_one(state=state, questions=QUESTIONS)
    except TypeSafeBadRequestError:
        log.warning("the model refused the sort for %s", row["id"], exc_info=True)
        return None
    ends = response.choices["ends"]
    return Answers(
        decision=response.nouls["decision"].noul,
        commitment=response.nouls["commitment"].noul,
        state=response.nouls["state"].noul,
        kind=response.choices["kind"].choice,
        ends=ends.choice,
        ends_confidence=ends.confidence,
    )


def place(answers: Answers, row: asyncpg.Record, settings: Settings) -> Placement:
    """Where the answers put a statement.

    The answers are read in an order, and the first yes says where the statement
    goes. A commitment comes first, because the decision question also says yes
    to most commitments, and the commitment question says no to decisions. A
    decision comes before the state of the work, because a decision that stays
    prospective still reaches a session for a while, and a decision that ends
    reaches none. What is left is a commitment with no condition it names.

    A statement with no moment to count from cannot end at "tomorrow", so it
    takes no moment.
    """
    if answers.commitment < settings.sort_commitment:
        if answers.decision >= settings.sort_decision:
            return Placement("decision", kind=answers.kind)
        if answers.state >= settings.sort_state:
            return Placement("state")
    if answers.ends == "event":
        return Placement("commitment", until_event=row["statement"])
    if row["said_at"] is None or answers.ends_confidence < settings.sort_moment:
        return Placement("commitment")
    return Placement(
        "commitment", until_moment=moments(row["said_at"], settings.time_zone).get(answers.ends)
    )


async def settle(pool: asyncpg.Pool, row: asyncpg.Record, placement: Placement | None) -> bool:
    """Write where a statement goes, and say whether the sort claimed it.

    Everything is one transaction, so a failure leaves the statement as it was
    and unsorted, and the next attempt asks again.

    A decision is written as a new row, and the prospective one retires into it
    through the pointer a replacement uses. The prospective row keeps what the
    writer wrote, and the chain says why the service reads it as a fact now.
    When the message already wrote a statement of that kind, the decision stays
    prospective. Retiring it into that statement would drop what it says,
    because the two are different sentences.

    No placement is a refusal. The provider refuses the same request again, so
    the statement is claimed and nothing else is written: it stays prospective
    and live, and it is not asked again. The ledger holds the refusal, and
    clearing `sorted_at` asks it again.
    """
    async with pool.acquire() as connection, connection.transaction():
        if await connection.fetchval(CLAIM, row["id"]) is None:
            log.info("%s was retired or sorted after it was listed, so it is left", row["id"])
            return False
        if placement is None:
            return True
        if placement.outcome == "decision":
            survivor = await connection.fetchval(REKIND, row["id"], placement.kind)
            if survivor is None:
                log.info(
                    "%s stays prospective, because its message already wrote a %s statement",
                    row["id"],
                    placement.kind,
                )
                return True
            await retire(connection, replaced=[row["id"]], replacement=survivor)
        elif placement.outcome == "state":
            await end(connection, ended=[row["id"]], reason="state")
        else:
            await connection.execute(
                CONDITION, row["id"], placement.until_moment, placement.until_event
            )
    return True


async def sort_one(
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    row: asyncpg.Record,
    *,
    settings: Settings,
    run: str,
) -> None:
    """Ask about one statement and write where it goes."""
    answers = await ask(client, row, settings, run)
    placement = None if answers is None else place(answers, row, settings)
    if await settle(pool, row, placement):
        log.info("sorted %s as %s", row["id"], placement)


async def sort(
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    rows: Sequence[asyncpg.Record],
    *,
    settings: Settings,
    run: str = "live",
    concurrency: int = 1,
) -> tuple[int, int]:
    """Sort these statements for a backfill. Returns how many were asked and how many failed.

    The calls are independent of each other, so a backfill runs several at once.
    A statement that fails is logged and left unsorted, and the rest go on, so
    one timeout does not stop a pass over a thousand statements.
    """
    limit = asyncio.Semaphore(concurrency)

    async def one(row: asyncpg.Record) -> bool:
        async with limit:
            try:
                await sort_one(pool, client, row, settings=settings, run=run)
            except Exception:
                log.exception("could not sort %s", row["id"])
                return False
        return True

    done = await asyncio.gather(*(one(row) for row in rows))
    return sum(done), len(done) - sum(done)


async def unsorted(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every live prospective statement the sort has not answered, oldest first."""
    return await pool.fetch(UNSORTED, None, None)


async def sort_message(
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    *,
    session_id: str,
    entry_id: str,
    settings: Settings,
    run: str = "live",
) -> int:
    """Sort the live prospective statements one message wrote. Returns how many.

    A failure is raised, so the worker tries the task again.
    """
    rows = await pool.fetch(UNSORTED, session_id, entry_id)
    for row in rows:
        await sort_one(pool, client, row, settings=settings, run=run)
    return len(rows)
