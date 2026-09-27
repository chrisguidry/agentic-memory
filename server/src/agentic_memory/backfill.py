"""Filling in what the worker writes for new rows, for the rows already stored.

The worker answers some things as it writes, and a store that predates them
needs them for every row it holds. `actionable` asks the System One model the
actionable question about every live statement with no answer, and records each
call in the ledger under a named run. `sort` asks it the sort's questions about
every live prospective statement the sort has not answered, and writes where
each one goes, under a named run the same way. `merge` compares every live
statement with the ones said before it, under the merge's current questions,
and records its calls the same way. `unmerge` undoes what one merge run
retired, with no model call. `readings` embeds the prompt of every reading,
with the local model and no network call.
"""

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime

import asyncpg

from .actionable import answer, price, unanswered
from .classify import model_client
from .db import open_pool
from .embed import TOKENS, load
from .ledger import RecordedSystemOne, priced
from .merge_pass import merge_backlog
from .merge_pass import price as merge_price
from .readings import BATCH, embed_readings
from .settings import get_settings
from .sorting import TASK as SORT
from .sorting import sort, unsorted
from .unmerge import WRITERS, Refused, undo


def moment(text: str) -> datetime:
    """A moment with its offset from UTC.

    A moment with no offset is refused, because the store and the person
    running the command can be in two time zones, and a window read in the
    wrong one restores the wrong retirements.
    """
    found = datetime.fromisoformat(text)
    if found.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text} has no offset from UTC, such as +00:00")
    return found


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(
        prog="agentic-memory-backfill",
        description="Fill in what the worker writes for new rows, for the rows already stored.",
    )
    kinds = found.add_subparsers(dest="backfill", required=True)

    statements = kinds.add_parser(
        "actionable",
        help="ask the actionable question about every live statement with no answer",
    )
    prospective = kinds.add_parser(
        "sort",
        help=(
            "sort every live prospective statement the sort has not answered into a"
            " decision, the state of the work, or a commitment"
        ),
    )
    for asking in (statements, prospective):
        asking.add_argument(
            "--run", required=True, help="the name the ledger records these calls under"
        )
        asking.add_argument(
            "--price",
            action="store_true",
            help="print what is left and what it would cost, from the calls already made, and stop",
        )
        asking.add_argument(
            "--limit", type=int, default=None, help="ask about at most this many, oldest first"
        )
        asking.add_argument(
            "--concurrency", type=int, default=8, help="how many calls are made at once"
        )

    merging = kinds.add_parser(
        "merge",
        help="merge every live statement with the ones said before it, oldest first",
    )
    merging.add_argument(
        "--run", required=True, help="the name the ledger records these calls under"
    )
    merging.add_argument(
        "--price",
        action="store_true",
        help="print the pairs the pass could ask and what they would cost, and stop",
    )
    merging.add_argument("--scope", default=None, help="take only the statements of this scope")
    merging.add_argument(
        "--limit", type=int, default=None, help="take at most this many, oldest first"
    )
    merging.add_argument(
        "--concurrency", type=int, default=8, help="how many calls are made at once"
    )
    merging.add_argument(
        "--again",
        action="store_true",
        help="compare the statements already compared too, as after a change to the questions",
    )

    unmerging = kinds.add_parser(
        "unmerge",
        help=(
            "make the statements one merge run retired live again, except the ones it"
            " retired into the same text, and clear the comparison mark on the statements"
            " it compared, so a rerun asks about them again"
        ),
    )
    unmerging.add_argument(
        "--run",
        required=True,
        help=(
            "the name of the merge run to undo. The runs the writer merges under are"
            f" refused: {', '.join(sorted(WRITERS))}, and any run with a classify or"
            " synthesize call in the ledger, such as a /reread run. Undoing one restores"
            " every statement the writer retired under it"
        ),
    )
    unmerging.add_argument(
        "--since",
        type=moment,
        default=None,
        help=(
            "also undo the merges that record no run, from this moment, such as"
            " 2026-09-27T06:00+00:00, up to --until. A merge made before the merge"
            " recorded its run records none, and neither does a correction or the"
            " sort. In the window, every retirement with no run whose survivor's text"
            " differs from its own is restored, a correction or a sort included, and"
            " every comparison mark with no run is cleared"
        ),
    )
    unmerging.add_argument(
        "--until",
        type=moment,
        default=None,
        help=(
            "the end of the --since window, which is not in it. Each needs the other,"
            " and the start comes before the end"
        ),
    )
    unmerging.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the statements an undo would restore and the marks it would clear,"
            " and change nothing"
        ),
    )

    prompts = kinds.add_parser(
        "readings", help="embed the prompt of every reading, with the local model"
    )
    # The batch is how many readings are read from the store at once, and a
    # reading's prompt is at most 10,000 characters, so it sets no memory worth
    # counting. The model's memory is set by `TOKENS`: it reads the batch in runs
    # of at most that many padded tokens, and a backfill of prompts up to 600
    # words peaked at 375 MB.
    prompts.add_argument(
        "--batch",
        type=int,
        default=BATCH,
        help=(
            f"how many readings are read from the store at once, {BATCH} by default."
            f" The model reads at most {TOKENS} tokens at a time, whatever the batch,"
            " and a backfill of long prompts peaked at 375 MB"
        ),
    )

    for subcommand in kinds.choices.values():
        subcommand.add_argument(
            "--database-url",
            default=None,
            help="the store to write, by default AGENTIC_MEMORY_DATABASE_URL",
        )
    return found


async def actionable(pool: asyncpg.Pool, arguments: argparse.Namespace) -> str:
    rows = await unanswered(pool)
    if arguments.limit is not None:
        rows = rows[: arguments.limit]
    if arguments.price:
        cost = await price(pool, len(rows))
        if cost is None:
            return f"{len(rows)} statements unanswered, and no calls to price them from\n"
        return (
            f"{len(rows)} statements unanswered, about {cost[0]} input and"
            f" {cost[1]} output tokens\n"
        )
    async with model_client() as client:
        count, failed = await answer(
            pool,
            RecordedSystemOne(client, pool),
            rows,
            run=arguments.run,
            concurrency=arguments.concurrency,
        )
    return (
        f"answered {count} of {len(rows)} statements under the run {arguments.run},"
        f" and {failed} failed\n"
    )


async def prospective(pool: asyncpg.Pool, arguments: argparse.Namespace) -> str:
    rows = await unsorted(pool)
    if arguments.limit is not None:
        rows = rows[: arguments.limit]
    if arguments.price:
        cost = await priced(pool, SORT, len(rows))
        if cost is None:
            return f"{len(rows)} prospective statements unsorted, and no calls to price them from\n"
        return (
            f"{len(rows)} prospective statements unsorted, about {cost[0]} input and"
            f" {cost[1]} output tokens\n"
        )
    async with model_client() as client:
        count, failed = await sort(
            pool,
            RecordedSystemOne(client, pool),
            rows,
            settings=get_settings(),
            run=arguments.run,
            concurrency=arguments.concurrency,
        )
    return (
        f"sorted {count} of {len(rows)} prospective statements under the run {arguments.run},"
        f" and {failed} failed\n"
    )


async def merge(pool: asyncpg.Pool, arguments: argparse.Namespace) -> str:
    settings = get_settings()
    if arguments.price:
        cost = await merge_price(
            pool,
            settings,
            run=arguments.run,
            scope=arguments.scope,
            limit=arguments.limit,
            again=arguments.again,
        )
        found = f"{cost.statements} statements and at most {cost.pairs} pairs to ask"
        if cost.tokens is None:
            return f"{found}, and no calls under the run {arguments.run} to price them from\n"
        return f"{found}, about {cost.tokens[0]} input and {cost.tokens[1]} output tokens\n"
    async with model_client() as client:
        passed = await merge_backlog(
            settings=settings,
            pool=pool,
            client=RecordedSystemOne(client, pool),
            run=arguments.run,
            scope=arguments.scope,
            limit=arguments.limit,
            concurrency=arguments.concurrency,
            again=arguments.again,
        )
    reasons = Counter(row.reason for row in passed.merged)
    across = sum(row.across for row in passed.merged)
    return (
        f"retired {len(passed.merged)} statements under the run {arguments.run}:"
        f" {reasons['cutoff']} above the upper cutoff, {reasons['same']} saying the same"
        f" thing, {reasons['settles']} settled again, {across} across kinds,"
        f" and {len(passed.failed)} statements failed and stay unmarked for a rerun\n"
    )


async def unmerge(pool: asyncpg.Pool, arguments: argparse.Namespace) -> str:
    try:
        undone = await undo(
            pool,
            run=arguments.run,
            since=arguments.since,
            until=arguments.until,
            dry_run=arguments.dry_run,
        )
    except Refused as refused:
        raise SystemExit(str(refused)) from None
    restore, clear = (
        ("would restore", "would clear") if arguments.dry_run else ("restored", "cleared")
    )
    return (
        f"{restore} {len(undone.restored)} statements the run {arguments.run} retired:"
        f" {' '.join(map(str, undone.restored))}\n"
        f"{clear} the comparison mark on {len(undone.reopened)} statements,"
        f" so a rerun asks about them again: {' '.join(map(str, undone.reopened))}\n"
    )


async def readings(pool: asyncpg.Pool, arguments: argparse.Namespace) -> str:
    settings = get_settings()
    embedder = await asyncio.to_thread(load, settings)
    count = await embed_readings(pool, embedder, arguments.batch)
    return f"embedded {count} readings with {embedder.model}\n"


async def run(arguments: argparse.Namespace) -> str:
    """Run one backfill and return what it did."""
    pool = await open_pool(arguments.database_url or get_settings().database_url)
    try:
        if arguments.backfill == "actionable":
            return await actionable(pool, arguments)
        if arguments.backfill == "sort":
            return await prospective(pool, arguments)
        if arguments.backfill == "merge":
            return await merge(pool, arguments)
        if arguments.backfill == "unmerge":
            return await unmerge(pool, arguments)
        return await readings(pool, arguments)
    finally:
        await pool.close()


def main() -> None:
    """Run a backfill from the command line."""
    sys.stdout.write(asyncio.run(run(parser().parse_args())))
