"""Whether a statement would change what an agent does.

Some statements are true and change nothing for the next piece of work. A
remark that one piece of work went well is the common one, and a short reply
such as "looks good" scores well against it, because the two are written in the
same register. The match would then hand the reply a statement about work that
is over.

So the System One model answers one more question about each statement, and
the probability is stored on the row. The match hands out only a statement that
clears a threshold. The ranking, the merge, and the opening list read every
live statement as before.

The question is asked off the turn path: by the worker when the writer writes a
statement, and by a backfill for the statements written before the question
existed.
"""

import asyncio
import logging
from collections.abc import Sequence

import asyncpg
from typesafe_sdk import Noul, TypeSafeBadRequestError

from .ledger import RecordedSystemOne, calling, priced
from .memories import live

log = logging.getLogger("agentic_memory.actionable")

# The task the ledger records these calls under.
TASK = "actionable"

# One yes/no proposition about one statement. The place is part of the state,
# because a fact about one repository changes what an agent does there and
# nothing anywhere else.
ACTIONABLE = Noul(
    instructions={
        "question": (
            "Would an agent starting new work in `place` act differently for knowing `statement`?"
        ),
        "inspect": "`statement`",
        "focus": (
            "`place` is where the agent works, and `everywhere` means any project. A "
            "standing rule, a fact about the code or the tools, and a correction change "
            "what an agent does. A remark that one piece of work went well, or a report "
            "that something finished, does not."
        ),
    },
    criteria={
        "true": "An agent would follow it, use it, or avoid something because of it.",
        "false": "It is about work that is over, and an agent would work the same without it.",
    },
)

# The live statements with no answer, of one message or of every message. A
# replaced or ended statement is never handed out, so it is not worth a call.
UNANSWERED = f"""
    SELECT id, statement, scope_key, session_id, entry_id
    FROM memories
    WHERE {live()}
      AND actionable IS NULL
      AND ($1::text IS NULL OR (session_id = $1 AND entry_id = $2))
    ORDER BY id
"""

STORE = "UPDATE memories SET actionable = $2 WHERE id = $1"


async def ask(client: RecordedSystemOne, row: asyncpg.Record, run: str) -> float | None:
    """The model's answer about one statement, or none when it refused.

    A refusal leaves the statement with no answer, so it is matched as it was
    before the question existed, and the ledger records the refusal.
    """
    state = {"statement": row["statement"], "place": row["scope_key"] or "everywhere"}
    try:
        with calling(TASK, session_id=row["session_id"], entry_id=row["entry_id"], run=run):
            response = await client.system_one(state=state, questions={TASK: ACTIONABLE})
    except TypeSafeBadRequestError:
        log.warning("the model refused the actionable question for %s", row["id"], exc_info=True)
        return None
    return response.nouls[TASK].noul


async def answer_one(
    pool: asyncpg.Pool, client: RecordedSystemOne, row: asyncpg.Record, run: str
) -> bool:
    """Answer the question for one statement and store the answer, unless it was refused."""
    probability = await ask(client, row, run)
    if probability is None:
        return False
    await pool.execute(STORE, row["id"], probability)
    return True


async def answer(
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    rows: Sequence[asyncpg.Record],
    *,
    run: str = "live",
    concurrency: int = 1,
) -> tuple[int, int]:
    """Answer the question for a backfill. Returns how many were answered and how many failed.

    The calls are independent of each other, so a backfill runs several at once.
    A statement that fails is logged and left unanswered, and the rest go on, so
    one timeout does not stop a pass over thousands of statements. A refusal is
    neither: it is logged and left unanswered.
    """
    limit = asyncio.Semaphore(concurrency)

    async def one(row: asyncpg.Record) -> bool | None:
        async with limit:
            try:
                return await answer_one(pool, client, row, run)
            except Exception:
                log.exception("could not answer the actionable question for %s", row["id"])
                return None

    done = await asyncio.gather(*(one(row) for row in rows))
    return done.count(True), done.count(None)


async def unanswered(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every live statement with no answer, oldest first."""
    return await pool.fetch(UNANSWERED, None, None)


async def answer_message(
    pool: asyncpg.Pool,
    client: RecordedSystemOne,
    *,
    session_id: str,
    entry_id: str,
    run: str = "live",
) -> int:
    """Answer the question for the live statements one message wrote. Returns how many.

    A failure is raised, so the worker tries the task again.
    """
    rows = await pool.fetch(UNANSWERED, session_id, entry_id)
    answered = 0
    for row in rows:
        answered += await answer_one(pool, client, row, run)
    return answered


async def price(pool: asyncpg.Pool, count: int) -> tuple[int, int] | None:
    """The input and output tokens `count` more calls would take, or none before any call.

    A statement is one sentence, so the calls are close to one size.
    """
    return await priced(pool, TASK, count)
