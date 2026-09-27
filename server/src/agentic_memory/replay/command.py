"""The replay as a command: a range, a moment, and a store, and the report."""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..db import open_pool
from ..embed import load
from ..settings import get_settings
from .report import labels, render, summarize, write_pairs
from .run import prompts, replay


def moment(text: str) -> datetime:
    """An ISO 8601 date or time, in UTC when it names no zone."""
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(
        prog="agentic-memory-replay",
        description=(
            "Run the prompts said in a range through the turn path as the code"
            " stands, against the statements live when each was said, and print"
            " what they were handed. Nothing is written to the store."
        ),
    )
    found.add_argument("--since", type=moment, required=True, help="the start of the range")
    found.add_argument(
        "--until", type=moment, required=True, help="the end of the range, not included"
    )
    found.add_argument(
        "--as-of",
        type=moment,
        required=True,
        help="the moment the store is read at: later prompts and later arrivals are left out",
    )
    found.add_argument(
        "--database-url",
        default=None,
        help="the store to read, by default AGENTIC_MEMORY_DATABASE_URL",
    )
    found.add_argument(
        "--pairs",
        type=Path,
        default=None,
        help="write each handed session, entry, and statement id here, sorted, one per line",
    )
    return found


async def run(arguments: argparse.Namespace) -> str:
    """Replay the range and return the report."""
    settings = get_settings()
    embedder = await asyncio.to_thread(load, settings)
    pool = await open_pool(arguments.database_url or settings.database_url)
    try:
        said = await prompts(
            pool, since=arguments.since, until=arguments.until, as_of=arguments.as_of
        )
        turns = await replay(pool, embedder, settings, said)
        given = await labels(pool)
    finally:
        await pool.close()
    if arguments.pairs is not None:
        write_pairs(arguments.pairs, turns)
    return render(summarize(turns, given))


def main() -> None:
    """Run the replay from the command line."""
    sys.stdout.write(asyncio.run(run(parser().parse_args())))
