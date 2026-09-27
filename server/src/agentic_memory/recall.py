"""The turn path: what a session is handed, and the record of it.

A turn starts, the client asks, and this answers. Every prompt is handed only
the statements that are about it, found by `match`, or nothing. A session's
first ask is also handed the top of its scope's list, from the statements that
have a scope. Both leave out what the session was already handed. No model is
called over a network here: the prompt is embedded in the process, and the rest
is reads of answers the worker wrote earlier, because a person is waiting.

Two filters come before the match. A prompt the harness wrote for itself is
handed nothing, and so is a short prompt whose nearest readings mostly held no
memory, which is how a reply with no subject of its own is recognised.

The handout is recorded by session so that two things can follow from it. A
statement the session already saw is not sent again, because the injected
text lands in the conversation and stays there. And the outcome flow, when it
is built, joins what a turn cost and whether it worked to what the turn was
handed, which is the only way the ranking learns.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import asyncpg

from .embed import Embedder
from .match import match
from .memories import live_at, ranked
from .metrics import RECALLS, phase
from .readings import holds_nothing, short
from .settings import Settings
from .window import plumbing

# The most statements one turn is handed. A turn's budget is the model's
# context, and past a few dozen sentences the injection is the conversation.
LIMIT = 50

# Every statement a session was handed. The turn path reads this once and
# passes it to the query that chooses, so the same query serves two sources of
# the list: the injections table for a live turn, and the replay's own record
# for a replayed one.
SEEN = """
    SELECT coalesce(array_agg(DISTINCT handed.id), '{}')
    FROM injections, unnest(memory_ids) AS handed(id)
    WHERE session_id = $1
"""

# The scope's list at a moment, less what this session was already handed.
#
# Only statements with a scope are on it, so the opening carries the standing
# rules for where the session is. A statement with no scope reaches a session
# only through the match, which reads the prompt. Without this, the same few
# statements with no scope, which rank high by kind, would open nearly every
# session whatever it is about. A session with no scope has no list.
UNSEEN = f"""
    SELECT id, statement, kind, score, scope_key, session_id, entry_id,
           model, said_at, created_at, actor, actor_depth
    FROM memories
    WHERE {live_at("$3", "$4")}
      AND scope_key IS NOT NULL
      AND (scope_key = $1 OR starts_with($1, scope_key || '/'))
      AND NOT (id = ANY($2::bigint[]))
"""

RECORD = """
    INSERT INTO injections (session_id, harness, scope_key, memory_ids, injected_at)
    VALUES ($1, $2, $3, $4::bigint[], $5)
"""

HANDED = """
    SELECT id, session_id, harness, scope_key, memory_ids, injected_at
    FROM injections
    WHERE session_id = $1
    ORDER BY injected_at DESC
    LIMIT $2
"""

# The forms a turn takes. `opening` is a session that has been handed nothing
# yet, which is handed the statements about the prompt and the top of the
# scope's list. `match` is every ask after that, handed only the statements
# about the prompt. `plumbing` is a prompt the harness wrote for itself, which is
# handed nothing.
Form = Literal["opening", "match", "plumbing"]


@dataclass(frozen=True)
class Handout:
    """What one turn is handed, and the form that chose it."""

    form: Form
    statements: list[dict]


def form_for(seen: Collection[int]) -> Form:
    """The form a turn takes, from what its session was handed before."""
    return "match" if seen else "opening"


async def seen_by(pool: asyncpg.Pool, session_id: str) -> frozenset[int]:
    """Every statement this session was handed."""
    return frozenset(await pool.fetchval(SEEN, session_id))


async def opening(
    pool: asyncpg.Pool,
    *,
    scope_key: str | None,
    seen: Collection[int],
    limit: int,
    now: datetime,
    as_of: datetime | None = None,
    by_said_at: bool = False,
) -> list[dict]:
    """The top of the scope's list at a moment, less what the session saw."""
    with phase("opening"):
        found = await pool.fetch(UNSEEN, scope_key, list(seen), as_of, by_said_at)
    return ranked((dict(row) for row in found), now)[: min(limit, LIMIT)]


async def choose(
    pool: asyncpg.Pool,
    embedder: Embedder,
    settings: Settings,
    *,
    seen: Collection[int],
    scope_key: str | None,
    prompt: str,
    now: datetime,
    as_of: datetime | None = None,
    by_said_at: bool = False,
) -> Handout:
    """What a turn is handed, chosen with nothing recorded.

    `now` is the moment the statements are aged to, and `as_of` is the moment
    the table is read at. A live turn reads the table as it is. A replay reads
    it as it stood when the prompt was said, or, with `by_said_at`, as it is
    now with each statement placed at the moment its message was said.

    A short prompt whose nearest readings mostly held no memory is handed
    nothing, the opening list included, so a session that opens with a reply gets its
    list on the first prompt that has a subject.
    """
    form = form_for(seen)
    matched: list[dict] = []
    if prompt.strip():
        with phase("embed"):
            vector = await asyncio.to_thread(embedder.query, prompt)
        if short(prompt, settings.recall_silence_words) and await holds_nothing(
            pool, vector, model=embedder.model, settings=settings, as_of=as_of
        ):
            return Handout(form, [])
        matched = await match(
            pool,
            vector,
            model=embedder.model,
            seen=seen,
            scope_key=scope_key,
            limit=settings.recall_prompt_limit,
            margin=settings.recall_margin,
            actionable=settings.recall_actionable,
            now=now,
            as_of=as_of,
            by_said_at=by_said_at,
        )
    if form == "match":
        return Handout(form, matched)
    listed = await opening(
        pool,
        scope_key=scope_key,
        seen=[*seen, *(row["id"] for row in matched)],
        limit=settings.recall_opening_limit,
        now=now,
        as_of=as_of,
        by_said_at=by_said_at,
    )
    return Handout(form, matched + listed)


async def record(
    pool: asyncpg.Pool,
    chosen: list[dict],
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    moment: datetime,
) -> None:
    """Write down what went, when anything did."""
    if chosen:
        with phase("record"):
            await pool.execute(
                RECORD, session_id, harness, scope_key, [row["id"] for row in chosen], moment
            )


async def turn(
    pool: asyncpg.Pool,
    embedder: Embedder,
    settings: Settings,
    *,
    session_id: str,
    harness: str,
    scope_key: str | None,
    prompt: str,
    now: datetime | None = None,
    waiting: Callable[[], Awaitable[bool]] | None = None,
) -> Handout:
    """What this turn is handed: the match, and the opening list on a session's first ask.

    `waiting` says whether the client is still waiting for the answer. A
    statement recorded for a client that stopped waiting never reached the
    session, and the record would keep the session from ever being handed it,
    so a handout is recorded only while the client waits.
    """
    moment = now or datetime.now(UTC)
    # A harness writes its own entries as prompts: a task notification, a
    # skill's body, a Stop hook's feedback. The classifier does not read them,
    # and a statement handed to one would be marked seen and never reach the
    # session again, so one is handed nothing before anything is read.
    if plumbing(prompt):
        RECALLS.labels(form="plumbing", outcome="nothing").inc()
        return Handout("plumbing", [])
    # A recall that fails before it reads what the session was handed has no
    # form yet, and its error is counted under this one.
    form = "unknown"
    try:
        with phase("seen"):
            handed_before = await seen_by(pool, session_id)
        form = form_for(handed_before)
        handout = await choose(
            pool,
            embedder,
            settings,
            seen=handed_before,
            scope_key=scope_key,
            prompt=prompt,
            now=moment,
        )
        # An empty handout writes nothing, so the check on the client runs only
        # when a write would follow.
        if handout.statements and waiting is not None and not await waiting():
            RECALLS.labels(form=form, outcome="gone").inc()
            return handout
        await record(
            pool,
            handout.statements,
            session_id=session_id,
            harness=harness,
            scope_key=scope_key,
            moment=moment,
        )
    except Exception:
        RECALLS.labels(form=form, outcome="error").inc()
        raise
    RECALLS.labels(form=form, outcome="handed" if handout.statements else "nothing").inc()
    return handout


async def handed(pool: asyncpg.Pool, *, session_id: str, limit: int = 50) -> list[dict]:
    """What a session was handed, latest turn first."""
    return [dict(row) for row in await pool.fetch(HANDED, session_id, limit)]
