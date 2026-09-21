#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["rich"]
# ///
"""Watch what the loop is producing.

Two panels. The top one is what is worth remembering, ranked, which is the
list a turn would read. The bottom one is what the classifier is reading
right now, so the list filling up can be seen happening.

It polls the service rather than reading the database, so it shows what a turn
would actually get. It reads the scope of the directory you run it from, the
same way a session derives its own, so the default view is what that directory
would be given.

    uv run tools/top.py
    uv run tools/top.py --all-scopes
    uv run tools/top.py --scope github.com/chrisguidry/agentic-memory --every 1
"""

import argparse
import json
import time
from datetime import UTC, datetime
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from scope import scope_of

console = Console()

# Two lines of a statement, near enough. A row that grows without a bound
# pushes everything under it off the screen, which is how the second panel
# disappeared in the first place.
STATEMENT_CHARS = 190


def two_lines(statement: str) -> str:
    """A statement cut to about two lines, with the cut marked."""
    said = " ".join(statement.split())
    if len(said) <= STATEMENT_CHARS:
        return said
    return said[:STATEMENT_CHARS].rstrip() + "\u2026"

KIND_STYLE = {
    "semantic": "cyan",
    "procedural": "green",
    "prospective": "yellow",
    "preference": "magenta",
    "correction": "red",
    "praise": "blue",
}


def fetch(endpoint: str, path: str, **params) -> list[dict]:
    """One GET, with the empty parameters left off the query."""
    wanted = {key: value for key, value in params.items() if value is not None}
    query = urllib.parse.urlencode(wanted)
    url = f"{endpoint.rstrip('/')}{path}" + (f"?{query}" if query else "")
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def memories_panel(rows: list[dict], scope: str | None) -> Panel:
    """What is worth remembering, ranked the way a turn would read it.

    One statement per row, with its kind and where it applies on the line
    above it. The statement wraps, so it reads as a sentence rather than as a
    fragment, and it is cut at two lines so one long statement cannot push the
    panel below it off the screen. The statement in full is a request away.
    """
    if not rows:
        body = Text("nothing yet. say something worth remembering.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=False, padding=(0, 1))
        table.add_column("", width=4, justify="right", style="dim")
        table.add_column("statement", overflow="fold", ratio=1)
        for row in rows:
            colour = KIND_STYLE.get(row["kind"], "white")
            where = row["scope_key"] or "everywhere"
            # A statement scoped above this one is reached by inheritance, and
            # saying so is the difference between a list of what is here and a
            # list of everything the turn was handed.
            inherited = scope is not None and where not in ("everywhere", scope)
            table.add_row(
                f"{row['score']:.2f}",
                Text(row["kind"], style=colour)
                + Text(f"  {where}{' \u2191' if inherited else ''}", style="dim")
                + Text("\n")
                + Text(two_lines(row["statement"])),
            )
        body = table

    return Panel(
        body,
        title="[bold]top of mind[/bold]"
        + (f" [dim]{scope}[/dim]" if scope else " [dim]all scopes[/dim]"),
        subtitle=f"[dim]{len(rows)} statement{'s' if len(rows) != 1 else ''}[/dim]",
        border_style="green",
    )


def ago(classified_at: str | None, now: datetime) -> str:
    """How long ago a reading was taken, in as few characters as it takes."""
    if not classified_at:
        return ""
    moment = datetime.fromisoformat(classified_at)
    seconds = int((now - moment).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def readings_panel(rows: list[dict]) -> Panel:
    """What the classifier read most recently, highest first."""
    if not rows:
        body = Text("nothing read yet.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=False, padding=(0, 1))
        table.add_column("when", width=8, style="dim", no_wrap=True, justify="right")
        table.add_column("what", ratio=1, no_wrap=True, overflow="ellipsis")
        now = datetime.now(UTC)
        for row in rows:
            when = ago(row["classified_at"], now)
            best = max((row[kind] for kind in KIND_STYLE if kind in row), default=0.0)
            winner = max(
                (kind for kind in KIND_STYLE if kind in row),
                key=lambda kind: row[kind],
                default="",
            )
            message = " ".join((row["message"] or "").split())
            table.add_row(
                when,
                Text(f"{best:.2f} ", style=KIND_STYLE.get(winner, "white"))
                + Text(winner, style=KIND_STYLE.get(winner, "white"))
                + Text(f"  {message}", style="dim"),
            )
        body = table

    return Panel(
        body,
        title="[bold]just read[/bold]",
        border_style="blue",
    )


def frame(args, state: dict) -> Group:
    """One redraw, from whatever the last poll returned."""
    if state.get("error"):
        top = Panel(Text(state["error"], style="red"), title="top of mind", border_style="red")
        bottom = Panel(Text(""), title="just read", border_style="red")
        return Group(top, bottom)

    return Group(
        memories_panel(state.get("memories", []), args.scope),
        readings_panel(state.get("readings", [])[: args.read]),
        Text(
            f"  {state.get('written', 0)} statements written · "
            f"{state.get('read', 0)} messages read · polling {args.endpoint}",
            style="dim",
        ),
    )


def poll(args, state: dict) -> None:
    """Get the two lists, and remember how they failed rather than raising."""
    try:
        state["memories"] = fetch(args.endpoint, "/memories", scope_key=args.scope, limit=args.limit)
        state["readings"] = fetch(args.endpoint, "/classifications", above=0.0, limit=args.read)
        state["read"] = len(state["readings"])
        state["written"] = len(state["memories"])
        state["error"] = None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as problem:
        state["error"] = f"cannot reach the service at {args.endpoint}: {problem}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4318")
    parser.add_argument(
        "--scope",
        default=None,
        help="a scope to read from. Defaults to this directory's scope.",
    )
    parser.add_argument(
        "--all-scopes",
        action="store_true",
        help="read from every scope, rather than this directory's",
    )
    parser.add_argument("--limit", type=int, default=10, help="how many statements to show")
    parser.add_argument("--read", type=int, default=6, help="how many recent readings to show")
    parser.add_argument("--every", type=float, default=2.0, help="seconds between polls")
    args = parser.parse_args()

    if args.all_scopes:
        args.scope = None
    elif args.scope is None:
        args.scope, named = scope_of(Path.cwd())
        console.print(f"[dim]scope {args.scope}, named by its {named}[/dim]")

    state: dict = {}
    with Live(frame(args, state), console=console, refresh_per_second=4) as live:
        while True:
            poll(args, state)
            live.update(frame(args, state))
            time.sleep(args.every)


if __name__ == "__main__":
    main()
