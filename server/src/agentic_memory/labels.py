"""Judging one turn's handout.

A label is not about the whole handout, it is about one pair: one prompt and
one statement the turn was given. That is why an injection knows nothing
about labelling, and why a sample can be redrawn against a store that has
moved on without disturbing a label already given. The pair outlives the
rules that produced it.

This module draws a sample of pairs from the record, serves it to the TUI a
pair at a time, and takes the person's judgment of each one.
"""

import argparse
import asyncio
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import asyncpg
from fastapi import APIRouter, Request
from pydantic import BaseModel

from .db import open_pool
from .settings import get_settings
from .window import NOT_PLUMBING

Label = Literal["good", "noise", "wrong"]
Form = Literal["opening", "match"]

# A prompt and its injection are written moments apart by the same turn, and
# this covers the gap between them. A prompt said any later than this belongs
# to the turn after, not the one being sampled.
PROMPT_SLACK = timedelta(seconds=5)


@dataclass(frozen=True)
class Pair:
    """One prompt and one statement it was handed, and what a sample draws on."""

    session_id: str
    entry_id: str
    memory_id: int
    form: Form
    kind: str
    scope_key: str | None

    @property
    def stratum(self) -> tuple[Form, str, bool]:
        """What the sample spreads its draws across."""
        return (self.form, self.kind, self.scope_key is not None)


def stratified_sample(
    pairs: Sequence[Pair], size: int, rng: random.Random | None = None
) -> list[Pair]:
    """About `size` pairs, spread evenly over form, kind, and whether a scope is set.

    A proportional draw hands the busiest stratum almost the whole sample and the
    rarest one none of it, and a first look at the turn path wants a few labelled
    examples of everything the code can produce, not a mirror of how often each
    thing happens. So every stratum with a candidate gets one before any gets a
    second, round after round, until the sample is full or every stratum is spent.
    """
    rng = rng or random.Random()
    grouped: dict[tuple[Form, str, bool], list[Pair]] = defaultdict(list)
    for pair in pairs:
        grouped[pair.stratum].append(pair)
    for group in grouped.values():
        rng.shuffle(group)

    order = sorted(grouped)
    quota = dict.fromkeys(order, 0)
    remaining = min(size, len(pairs))
    while remaining > 0:
        gave = False
        for key in order:
            if remaining <= 0:
                break
            if quota[key] < len(grouped[key]):
                quota[key] += 1
                remaining -= 1
                gave = True
        if not gave:
            break
    return [pair for key in order for pair in grouped[key][: quota[key]]]


# Every injection in the record, with whether it was the first its session
# ever got. The row number is computed over the whole table, unfiltered,
# because a session's opening handout can fall before the range a sample is
# drawn from while a later match from the same session falls inside it.
RANKED_INJECTIONS = """
    WITH ranked AS (
        SELECT id, session_id, injected_at, memory_ids,
               row_number() OVER (PARTITION BY session_id ORDER BY injected_at, id) = 1
                   AS opening
        FROM injections
    )
    SELECT id, session_id, injected_at, memory_ids, opening
    FROM ranked
    WHERE injected_at >= $1 AND injected_at < $2
    ORDER BY session_id, injected_at
"""

# The real, human prompts of a set of sessions, oldest first. A subagent's
# prompt carries `kind = 'prompt'` too, and it is not what a person typed, so
# only depth zero is read here.
PROMPTS_BY_SESSION = f"""
    SELECT session_id, occurred_at, entry_id
    FROM logs
    WHERE session_id = ANY($1::text[]) AND kind = 'prompt' AND actor = 'human'
      AND entry_id IS NOT NULL
      AND {NOT_PLUMBING}
    ORDER BY session_id, occurred_at
"""

MEMORY_STRATA = "SELECT id, kind, scope_key FROM memories WHERE id = ANY($1::bigint[])"


async def _prompts_by_session(
    pool: asyncpg.Pool, session_ids: set[str]
) -> dict[str, list[tuple[datetime, str]]]:
    if not session_ids:
        return {}
    found = await pool.fetch(PROMPTS_BY_SESSION, list(session_ids))
    by_session: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for row in found:
        by_session[row["session_id"]].append((row["occurred_at"], row["entry_id"]))
    return by_session


def _prompt_for(prompts: list[tuple[datetime, str]], injected_at: datetime) -> str | None:
    """The latest real prompt at or before an injection, allowing a little clock skew."""
    deadline = injected_at + PROMPT_SLACK
    entry_id = None
    for occurred_at, found in prompts:
        if occurred_at > deadline:
            break
        entry_id = found
    return entry_id


async def _memory_strata(pool: asyncpg.Pool, memory_ids: set[int]) -> dict[int, asyncpg.Record]:
    if not memory_ids:
        return {}
    found = await pool.fetch(MEMORY_STRATA, list(memory_ids))
    return {row["id"]: row for row in found}


async def candidate_pairs(pool: asyncpg.Pool, *, since: datetime, until: datetime) -> list[Pair]:
    """Every prompt-and-statement pair a turn was handed in a range.

    Pairing an injection to its prompt reads the record, not the injection: the
    row only carries when the turn happened and what it got, and the prompt is
    whichever real, human words the session had said last before that moment.
    An injection with no such prompt near it, or whose statement is not a
    statement the store still knows, contributes nothing.
    """
    injected = await pool.fetch(RANKED_INJECTIONS, since, until)
    if not injected:
        return []

    prompts = await _prompts_by_session(pool, {row["session_id"] for row in injected})
    memory_ids = {memory_id for row in injected for memory_id in row["memory_ids"]}
    strata = await _memory_strata(pool, memory_ids)

    pairs: list[Pair] = []
    for row in injected:
        entry_id = _prompt_for(prompts.get(row["session_id"], []), row["injected_at"])
        if entry_id is None:
            continue
        form: Form = "opening" if row["opening"] else "match"
        for memory_id in row["memory_ids"]:
            found = strata.get(memory_id)
            if found is None:
                continue
            pairs.append(
                Pair(
                    session_id=row["session_id"],
                    entry_id=entry_id,
                    memory_id=memory_id,
                    form=form,
                    kind=found["kind"],
                    scope_key=found["scope_key"],
                )
            )
    return pairs


INSERT_PAIRS = """
    INSERT INTO label_pairs (sample, session_id, entry_id, memory_id, form)
    SELECT $1, * FROM unnest($2::text[], $3::text[], $4::bigint[], $5::text[])
    ON CONFLICT (sample, session_id, entry_id, memory_id) DO NOTHING
"""


async def build_sample(
    pool: asyncpg.Pool,
    *,
    name: str,
    since: datetime,
    until: datetime,
    size: int = 150,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Draw a sample of pairs from a range of the injections, and store it under a name.

    Building the same name twice adds nothing the first draw did not: the pair
    is what a row conflicts on, so a sample is a fixed list once it exists, not
    something that grows every time the command runs.
    """
    candidates = await candidate_pairs(pool, since=since, until=until)
    drawn = set(stratified_sample(candidates, size, rng))
    # Restored to the order the record happened in, because the stratified draw
    # groups by stratum and would otherwise hand the TUI turns out of order.
    chosen = [found for found in candidates if found in drawn]
    if chosen:
        await pool.execute(
            INSERT_PAIRS,
            name,
            [pair.session_id for pair in chosen],
            [pair.entry_id for pair in chosen],
            [pair.memory_id for pair in chosen],
            [pair.form for pair in chosen],
        )
    return {"candidates": len(candidates), "sampled": len(chosen)}


# Whether a pair has been judged is asked by joining to `labels` on the triple
# that names a pair, and not by a foreign key from `label_pairs`, because the
# same pair can be drawn into more than one sample and a label belongs to the
# pair rather than to whichever sample happened to draw it.
COUNTS = """
    SELECT count(*) AS total,
           count(*) FILTER (
               WHERE EXISTS (
                   SELECT 1 FROM labels l
                   WHERE l.session_id = lp.session_id AND l.entry_id = lp.entry_id
                     AND l.memory_id = lp.memory_id
               )
           ) AS done
    FROM label_pairs lp
    WHERE lp.sample = $1
"""

NEXT_UNLABELLED = """
    SELECT lp.id, lp.session_id, lp.entry_id, lp.memory_id
    FROM label_pairs lp
    WHERE lp.sample = $1 AND lp.id > $2
      AND NOT EXISTS (
          SELECT 1 FROM labels l
          WHERE l.session_id = lp.session_id AND l.entry_id = lp.entry_id
            AND l.memory_id = lp.memory_id
      )
    ORDER BY lp.id
    LIMIT 1
"""

PROMPT_TEXT = (
    "SELECT body FROM logs WHERE session_id = $1 AND entry_id = $2 AND kind = 'prompt' LIMIT 1"
)

STATEMENT = "SELECT statement, kind, scope_key, said_at FROM memories WHERE id = $1"


async def next_unlabelled(pool: asyncpg.Pool, *, sample: str, after: int = 0) -> dict:
    """The next pair of a sample nobody has judged, and how far the sample has come.

    A pair already labelled under another sample counts as done here too: the
    label belongs to the pair, not to the sample that happened to draw it.

    `after` moves past a pair without judging it, and once every later pair is
    spoken for it wraps back to the start, so skipping revisits what was
    skipped instead of losing it.
    """
    counts = await pool.fetchrow(COUNTS, sample)
    found = await pool.fetchrow(NEXT_UNLABELLED, sample, after)
    if found is None and after:
        found = await pool.fetchrow(NEXT_UNLABELLED, sample, 0)
    if found is None:
        return {"done": counts["done"], "total": counts["total"], "pair": None}

    prompt = await pool.fetchval(PROMPT_TEXT, found["session_id"], found["entry_id"])
    statement = await pool.fetchrow(STATEMENT, found["memory_id"])
    return {
        "done": counts["done"],
        "total": counts["total"],
        "pair": {
            "id": found["id"],
            "session_id": found["session_id"],
            "entry_id": found["entry_id"],
            "memory_id": found["memory_id"],
            "prompt": prompt or "",
            "statement": statement["statement"] if statement else "",
            "kind": statement["kind"] if statement else None,
            "scope_key": statement["scope_key"] if statement else None,
            "said_at": statement["said_at"].isoformat()
            if statement and statement["said_at"]
            else None,
        },
    }


WRITE_LABEL = """
    INSERT INTO labels (session_id, entry_id, memory_id, label, labelled_at)
    VALUES ($1, $2, $3, $4, now())
    ON CONFLICT (session_id, entry_id, memory_id)
    DO UPDATE SET label = EXCLUDED.label, labelled_at = EXCLUDED.labelled_at
"""


async def write_label(
    pool: asyncpg.Pool, *, session_id: str, entry_id: str, memory_id: int, label: Label
) -> None:
    """Judge one pair, replacing whatever this pair was judged before."""
    await pool.execute(WRITE_LABEL, session_id, entry_id, memory_id, label)


router = APIRouter()


class Judgment(BaseModel):
    """What the TUI sends after a key press."""

    session_id: str
    entry_id: str
    memory_id: int
    label: Label


@router.get("/labels/next")
async def next_pair(request: Request, sample: str, after: int = 0) -> dict:
    """The next pair of a sample nobody has judged, and how far it has come."""
    return await next_unlabelled(request.app.state.pool, sample=sample, after=after)


@router.post("/labels")
async def label_pair(request: Request, judgment: Judgment) -> dict:
    """Judge one pair. A second judgment of the same pair replaces the first."""
    await write_label(
        request.app.state.pool,
        session_id=judgment.session_id,
        entry_id=judgment.entry_id,
        memory_id=judgment.memory_id,
        label=judgment.label,
    )
    return {"labelled": judgment.label}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draw a stratified sample of prompt-and-statement pairs to label."
    )
    parser.add_argument("name", help="the sample's name")
    parser.add_argument("--since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--until", required=True, type=datetime.fromisoformat)
    parser.add_argument("--size", type=int, default=150)
    return parser.parse_args(argv)


async def _build(args: argparse.Namespace) -> dict[str, int]:
    pool = await open_pool(get_settings().database_url)
    try:
        return await build_sample(
            pool, name=args.name, since=args.since, until=args.until, size=args.size
        )
    finally:
        await pool.close()


def main() -> None:
    """Draw and store a sample: `uv run agentic-memory-sample NAME --since ... --until ...`."""
    args = _parse_args()
    result = asyncio.run(_build(args))
    print(f"{result['sampled']} of {result['candidates']} candidates sampled into {args.name!r}")


if __name__ == "__main__":
    main()
