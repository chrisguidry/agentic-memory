#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Load the sessions a harness already wrote into the service.

Each harness has a module under `harnesses/`. This command asks one of them
which files a source holds, reads them, and sends what it finds as OTLP log
records. Adding a harness means adding a module and naming it in the registry.

    uv run tools/backfill.py --harness pi
    uv run tools/backfill.py --harness claude-code
    uv run tools/backfill.py --harness codex --machine desktop --from /mnt/old/.codex/sessions

The machine defaults to this host's name. Name it when the transcripts came
from somewhere else, or every record will say the wrong machine, and a record
that names the wrong machine is worse than one that names none, because it
looks right.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harnesses import HARNESSES
from otlp import DEFAULT_ENDPOINT, default_machine, post

BATCH = 200


def send(
    endpoint: str, machine: str, batch: list[dict[str, Any]], counted: dict[str, int]
) -> None:
    """Send one batch, and add what the service did with it to the counts."""
    if not batch:
        return
    answer = post(endpoint, machine, batch)
    counted["inserted"] += answer.get("inserted", 0)
    counted["repeated"] += answer.get("repeated", 0)
    counted["failed"] += answer.get("failed", 0)
    batch.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=sorted(HARNESSES))
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        help="a session directory, or a copy of one from another machine",
    )
    parser.add_argument(
        "--machine",
        help="the machine the transcripts came from. Defaults to this host's name.",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--limit", type=int, help="stop after this many files")
    parser.add_argument("--dry-run", action="store_true", help="read, and send nothing")
    options = parser.parse_args()

    harness = HARNESSES[options.harness]
    source: Path = options.source or harness.DEFAULT_SOURCE
    machine = options.machine or default_machine()

    files = harness.discover(source)
    if options.limit:
        files = files[: options.limit]
    if not files:
        print(f"no {options.harness} sessions under {source}", file=sys.stderr)
        return 1

    print(f"{len(files)} {options.harness} session files under {source}")
    print(f"reading them as {machine}, sending to {options.endpoint}")

    counted = {"inserted": 0, "repeated": 0, "failed": 0}
    batch: list[dict[str, Any]] = []
    started = datetime.now(UTC)

    for number, path in enumerate(files, start=1):
        for record in harness.read(path, machine):
            batch.append(record)
            if len(batch) >= options.batch and not options.dry_run:
                send(options.endpoint, machine, batch, counted)
        if number % 100 == 0 or number == len(files):
            elapsed = (datetime.now(UTC) - started).total_seconds()
            print(
                f"  {number}/{len(files)} files, {counted['inserted']} new, "
                f"{counted['repeated']} repeats, {counted['failed']} refused, {elapsed:.0f}s"
            )

    if not options.dry_run:
        send(options.endpoint, machine, batch, counted)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(
        f"done in {elapsed:.0f}s: {counted['inserted']} new records, "
        f"{counted['repeated']} already held, {counted['failed']} refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
