"""Merging statements that say the same thing, or that settle one question.

The table holds one rule many times in different words, and the match cannot
pass a rule that is its own baseline, and a later message can change a
decision an older statement records. A statement is compared with its nearest
live neighbours of the same kind in the same scope, and the older of a pair
retires into the newer through the pointer plan 03 built. Nothing is deleted or
rewritten.

One decision is also often written twice, once as praise when the person
approves it and once as a fact when it is settled. A setting compares a
statement with neighbours of every kind, which merges the two, and the survivor
keeps its own kind. The setting is off, because the first question also merges
statements of two kinds that are only about the same area.

Two statements at or above the upper cutoff are one sentence with a word moved,
and they merge on the number alone. Between the upper and the lower cutoff an
embedding cannot tell a negation from its opposite, so the System One model is
asked two questions: whether the two say the same thing, and whether the newer
one settles the question the older one settled, in a different way. Below the
lower cutoff they are different enough that nothing is compared.

Two rules keep a pair apart. A statement retires only into one whose kind
lasts at least as long: the four durable kinds into each other, a plan into a
plan or a durable kind, and praise into anything. A statement retires into one
that says the same thing, by the upper cutoff or by the first question, only
when that one holds all its literals, such as a URL or a flag. Without them, a
specific fact retires into a general or approving statement said after it, and
no live statement holds the fact. A pair the first question calls the same
thing stands when a literal is lacking, whatever the second question says. The
second question otherwise keeps only the first rule, because a newer statement
that settles an older one in a different way changes a value on purpose.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import asyncpg
from typesafe_sdk import Noul, TypeSafeBadRequestError

from .ledger import calling
from .literals import lacks
from .memories import live, retire
from .settings import Settings

log = logging.getLogger("agentic_memory.merge")

# The first question the classifier's System One model answers about a pair. It
# is one yes/no proposition, so it needs no threshold of its own: the model's
# probability is read as a yes above one half.
SAME = Noul(
    instructions={
        "question": (
            "Do `first` and `second` say the same thing, and does neither one "
            "forbid what the other allows?"
        ),
        "inspect": "`first` and `second`",
        "focus": (
            "Two statements say the same thing when a reader who believes one is "
            "right to believe the other. One may add a detail. One may not permit "
            "what the other forbids, and one may not be the negation of the other."
        ),
    },
    criteria={
        "true": "They agree, and neither permits what the other forbids.",
        "false": "They differ in what they require, permit, or forbid.",
    },
)

# The second question, asked in the same call. A newer statement that changes
# an older one answers no to the first question, and without this one both
# stand, so a reader is handed two statements that disagree with nothing to say
# which is current. A detail added to the older statement, or a statement about
# another thing, answers no here, because retiring the older one would lose it.
SETTLES = Noul(
    instructions={
        "question": (
            "Does `second` settle the same question as `first`, and settle it in a different way?"
        ),
        "inspect": "`first` and `second`",
        "focus": (
            "`second` was said after `first`. Both settle one question when they "
            "answer the same choice about the same thing, such as which tool, which "
            "value, which place, or which rule. `second` settles it in a different "
            "way when a reader who follows `second` can no longer follow `first`. A "
            "`second` that only adds a detail to `first`, or that answers a choice "
            "about another thing, does not."
        ),
    },
    criteria={
        "true": "Both answer one choice about one thing, and `second` answers it differently.",
        "false": "They answer different choices, or `second` keeps what `first` says.",
    },
)

QUESTIONS = {"same": SAME, "settles": SETTLES}

# The point on the model's probability above which the first answer is a yes.
YES = 0.5

# Why the older statement of a pair retired: the similarity alone, or a yes to
# one of the two questions.
Reason = Literal["cutoff", "same", "settles"]


@dataclass(frozen=True)
class Merged:
    """One statement retired into another, why, and whether their kinds differ."""

    retired: int
    survivor: int
    reason: Reason
    across: bool


class Judge(Protocol):
    """A System One client: the recorded one, or one that limits calls at once."""

    async def system_one(self, *, state: Any, questions: Any) -> Any: ...


# One live statement embedded with the model being compared, with its kind, its
# scope, and the moment it was said. A statement with no moment cannot be placed
# in order, so it is never merged. A commitment past its moment has ended, so it
# neither merges nor survives a merge.
SUBJECT = f"""
    SELECT id, statement, kind, scope_key, said_at, session_id, entry_id
    FROM memories
    WHERE id = $1
      AND {live()}
      AND (until_moment IS NULL OR until_moment > now())
      AND embedding IS NOT NULL
      AND embedding_model = $2
      AND said_at IS NOT NULL
"""

# The live neighbours of that statement: the same scope, embedded with the same
# model, placed in time, at or above the lower cutoff, nearest first. A null
# scope is its own scope, because "tests come before code" in one project and in
# another are one rule stated twice and merging them would move where the rule
# applies.
#
# `$4` keeps only the neighbours said before the statement. The pass over the
# table sets it, so each pair is asked about once, when the pass reaches the
# newer statement of the pair.
#
# `$5` admits neighbours of every kind. Without it, only the statement's own
# kind is a candidate, because the first question merges statements of two
# kinds that are only about the same area.
NEIGHBOURS = f"""
    SELECT m.id, m.statement, m.kind, m.scope_key, m.said_at,
           1 - (m.embedding <=> s.embedding) AS similarity
    FROM memories m
    JOIN memories s ON s.id = $1
    WHERE {live("m")}
      AND (m.until_moment IS NULL OR m.until_moment > now())
      AND m.id <> $1
      AND m.scope_key IS NOT DISTINCT FROM s.scope_key
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $2
      AND m.said_at IS NOT NULL
      AND 1 - (m.embedding <=> s.embedding) >= $3
      AND (NOT $4::boolean OR (m.said_at, m.id) < (s.said_at, s.id))
      AND ($5::boolean OR m.kind = s.kind)
    ORDER BY m.embedding <=> s.embedding
"""

# Marks a statement as compared with its neighbours. The writer's task and the
# pass merge the statements that have no mark, so a retry finishes what a
# failed attempt left, and no pair is asked about twice. The run is recorded so
# an undo of that run clears the mark and a rerun asks again.
COMPARED = "UPDATE memories SET merged_at = now(), merged_by_run = $2 WHERE id = $1"


def placed(row: Any) -> tuple:
    """Where a statement falls in the order it was said."""
    return (row["said_at"], row["id"])


async def answers(client: Judge, older: str, newer: str) -> dict[str, float] | None:
    """The model's answers to both questions about a pair, or none when it refused.

    A refusal is read as a no to both, so the two statements both stand. The
    ledger records the refusal, and the pass goes on rather than failing on a
    request the provider will refuse again.
    """
    try:
        response = await client.system_one(
            state={"first": older, "second": newer}, questions=QUESTIONS
        )
    except TypeSafeBadRequestError:
        log.warning("the model refused the merge questions", exc_info=True)
        return None
    return {name: response.nouls[name].noul for name in QUESTIONS}


# How long each kind of statement stays true, longest first. A statement
# retires only into one whose kind lasts at least as long, because the survivor
# is all a reader keeps. A rule retired into a plan fades in weeks or ends when
# the plan's moment passes, and a plan retired into praise is gone, because
# praise approves an outcome and holds less than the plan it approves.
#
# The four durable kinds are one tier, so a fact and the rule it becomes still
# merge. A kind with no entry merges with nothing, because nobody has decided
# how long it lasts, and a new kind that could retire into praise would vanish
# before anyone noticed it was missing here.
#
# `RANKING` in `memories.py` is a different order, so this one is declared
# here. Its weights and half-lives measure how long a statement is worth
# reading, and they put praise above prospective. Its floors set the durable
# kinds apart from the others and make praise and prospective equal.
LASTING = {
    "correction": 2,
    "preference": 2,
    "procedural": 2,
    "semantic": 2,
    "prospective": 1,
    "praise": 0,
}


def into_shorter(older: Any, newer: Any) -> bool:
    """Whether the older statement would retire into a kind that lasts less.

    A pair where either kind has no entry is kept apart, as though it would.
    """
    if older["kind"] not in LASTING or newer["kind"] not in LASTING:
        return True
    return LASTING[newer["kind"]] < LASTING[older["kind"]]


async def why(client: Judge, subject: Any, neighbour: Any, settings: Settings) -> Reason | None:
    """Why the older of two statements retires into the newer, or none when both stand.

    A pair that says the same thing merges only when the newer statement holds
    every literal of the older one, because a general statement that retires a
    specific one loses the details a reader copies. A newer statement that
    settles the older one in a different way changes a value on purpose, such
    as port 8080 to 9090, so that merge does not ask for the literals. It is
    asked only when the first question says no, because a pair that says the
    same thing without a literal is not a changed value.
    """
    older, newer = sorted([subject, neighbour], key=placed)
    # This rule holds at any similarity and under both questions, so it comes
    # first, and a pair it keeps apart costs no call.
    if into_shorter(older, newer):
        log.debug(
            "%s %s stays beside %s %s", older["kind"], older["id"], newer["kind"], newer["id"]
        )
        return None
    lacking = lacks(older["statement"], newer["statement"])
    # A pair above the upper cutoff that differs in a literal is one sentence
    # with a value changed, so the model is asked whether the newer one settles
    # the older.
    if neighbour["similarity"] >= settings.merge_upper and not lacking:
        return "cutoff"
    found = await answers(client, older["statement"], newer["statement"])
    if found is None:
        return None
    if found["same"] >= YES:
        # A yes here with a literal lacking is a general statement of the same
        # thing, not a new value. The second question would retire the older
        # one into it and lose the literal, so the pair stands.
        return None if lacking else "same"
    # Two statements said at one moment, such as two kinds from one message,
    # have no newer one, so neither can have changed the other's answer.
    if newer["said_at"] > older["said_at"] and found["settles"] >= settings.merge_settles:
        return "settles"
    return None


async def merge(
    pool: asyncpg.Pool,
    client: Judge,
    *,
    statement_id: int,
    model: str,
    settings: Settings,
    run: str = "live",
    earlier: bool = False,
) -> list[Merged]:
    """Merge one statement with its neighbours, and say which ones ended.

    A statement retires only into a statement it was compared with. The older
    neighbours that merge retire into the statement, and the statement retires
    into the newest of the newer neighbours that merge. Two neighbours were
    never compared with each other, so neither retires into the other, and the
    newer neighbours that the statement does not retire into stay live.

    The write merges the newest statement, so every older duplicate points
    straight at it. The statement is marked compared in the same transaction
    that stores the retirements, so a failure before the commit leaves it
    unmarked and a retry compares it again.

    `earlier` compares the statement only with the ones said before it.
    """
    subject = await pool.fetchrow(SUBJECT, statement_id, model)
    if subject is None:
        return []

    neighbours = await pool.fetch(
        NEIGHBOURS,
        statement_id,
        model,
        settings.merge_lower,
        earlier,
        settings.merge_across_kinds,
    )

    with calling(
        "merge",
        session_id=subject["session_id"],
        entry_id=subject["entry_id"],
        run=run,
    ):
        # Every call finishes before a failure is raised, so no call is left
        # running after the merge has given up on the statement.
        reasons = await asyncio.gather(
            *(why(client, subject, neighbour, settings) for neighbour in neighbours),
            return_exceptions=True,
        )
    for reason in reasons:
        if isinstance(reason, BaseException):
            raise reason

    joined = [
        (neighbour, reason)
        for neighbour, reason in zip(neighbours, reasons, strict=True)
        if reason is not None
    ]
    older = [(row, reason) for row, reason in joined if placed(row) < placed(subject)]
    newer = [(row, reason) for row, reason in joined if placed(row) > placed(subject)]
    # Older first, so each older neighbour retires while the statement is still
    # live, and then the statement retires into the newer one.
    pairs = [(row, subject, reason) for row, reason in older]
    if newer:
        survivor, reason = max(newer, key=lambda pair: placed(pair[0]))
        pairs.append((subject, survivor, reason))

    merged = []
    async with pool.acquire() as connection, connection.transaction():
        for retiring, survivor, reason in pairs:
            ended = await retire(
                connection, replaced=[retiring["id"]], replacement=survivor["id"], run=run
            )
            merged += [
                Merged(
                    retired=retiring["id"],
                    survivor=survivor["id"],
                    reason=reason,
                    across=retiring["kind"] != survivor["kind"],
                )
                for _ in ended
            ]
        await connection.execute(COMPARED, statement_id, run)
    if merged:
        log.info("merged %s statements with %s", len(merged), statement_id)
    return merged


# The live statements one message wrote that have not been compared, which is
# what a write merges after it embeds them. The model is the one being compared,
# so a statement embedded before a model change is not merged until it is
# embedded again.
WRITTEN = f"""
    SELECT m.id
    FROM memories m
    WHERE m.session_id = $1
      AND m.entry_id = $2
      AND {live("m")}
      AND m.merged_at IS NULL
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $3
    ORDER BY m.id
"""


async def merge_message(
    pool: asyncpg.Pool,
    client: Judge,
    *,
    session_id: str,
    entry_id: str,
    model: str,
    settings: Settings,
    run: str = "live",
) -> int:
    """Merge what one message wrote, after its statements have been embedded."""
    written = await pool.fetch(WRITTEN, session_id, entry_id, model)
    merged = 0
    for row in written:
        merged += len(
            await merge(
                pool, client, statement_id=row["id"], model=model, settings=settings, run=run
            )
        )
    return merged
