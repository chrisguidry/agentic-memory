"""Filling in what the worker writes for new rows, for the rows already stored.

The worker answers two things as it writes, and a store that predates them
needs them for every row it holds. `actionable` asks the System One model the actionable question
about every live statement with no answer, and records each call in the ledger
under a named run. `readings` embeds the prompt of every reading, with the
local model and no network call.
"""

import argparse
import asyncio
import sys

import asyncpg

from .actionable import answer, price, unanswered
from .classify import model_client
from .db import open_pool
from .embed import load
from .ledger import RecordedSystemOne
from .readings import BATCH, embed_readings
from .settings import get_settings


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
    statements.add_argument(
        "--run", required=True, help="the name the ledger records these calls under"
    )
    statements.add_argument(
        "--price",
        action="store_true",
        help="print what is left and what it would cost, from the calls already made, and stop",
    )
    statements.add_argument(
        "--limit", type=int, default=None, help="answer at most this many, oldest first"
    )
    statements.add_argument(
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
        count = await answer(
            pool,
            RecordedSystemOne(client, pool),
            rows,
            run=arguments.run,
            concurrency=arguments.concurrency,
        )
    return f"answered {count} of {len(rows)} statements under the run {arguments.run}\n"


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
        return await readings(pool, arguments)
    finally:
        await pool.close()


def main() -> None:
    """Run a backfill from the command line."""
    sys.stdout.write(asyncio.run(run(parser().parse_args())))
