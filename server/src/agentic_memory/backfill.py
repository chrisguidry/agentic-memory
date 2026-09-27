"""Filling in what the worker writes for new rows, for the rows already stored.

The worker answers three things as it writes, and a store that predates them
needs them for every row it holds. `actionable` asks the System One model the actionable question
about every live statement with no answer, and records each call in the ledger
under a named run. `sort` asks it the sort's questions about every live
prospective statement the sort has not answered, and writes where each one
goes, under a named run the same way. `readings` embeds the prompt of every
reading, with the local model and no network call.
"""

import argparse
import asyncio
import sys

import asyncpg

from .actionable import answer, price, unanswered
from .classify import model_client
from .db import open_pool
from .embed import load
from .ledger import RecordedSystemOne, priced
from .readings import BATCH, embed_readings
from .settings import get_settings
from .sorting import TASK as SORT
from .sorting import sort, unsorted


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

    prompts = kinds.add_parser(
        "readings", help="embed the prompt of every reading, with the local model"
    )
    # The model pads a batch to its longest prompt, so a batch of pasted files
    # holds hundreds of megabytes at once. A pod with a small memory limit runs
    # this with a small batch, which is slower and stays under the limit.
    prompts.add_argument(
        "--batch",
        type=int,
        default=BATCH,
        help=f"how many prompts are embedded at once, {BATCH} by default",
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
        return await readings(pool, arguments)
    finally:
        await pool.close()


def main() -> None:
    """Run a backfill from the command line."""
    sys.stdout.write(asyncio.run(run(parser().parse_args())))
