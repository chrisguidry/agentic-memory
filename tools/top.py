#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["rich"]
# ///
"""Watch what the loop is producing.

Three panels, because the pipeline has three places to look. The top one is what
is worth remembering, ranked, which is the list a turn would read. The middle
one is what the writer has just produced, in the order it produced it, so a
statement that ranks low is still seen arriving. The bottom one is what the
classifier is reading, which includes the messages it read and wrote nothing
for.

The last two are what make a message that never became a memory visible. A
message below every threshold produces no row, so only the readings panel can
show it.

It polls the service rather than reading the database, so it shows what a turn
would actually get. It reads the scope of the directory you run it from, the
same way a session derives its own, so the default view is what that directory
would be given.

Three panels of ten want fifty-five rows of terminal, so each count is an upper
bound and the frame gives way from the top down until it fits. The status line
says how many were hidden.

    uv run tools/top.py
    uv run tools/top.py --all-scopes
    uv run tools/top.py --scope github.com/chrisguidry/agentic-memory --every 1
    uv run tools/top.py --limit 5 --new 5 --read 5
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

    One statement per row, with its kind, where it applies, and how long ago it
    was written on the line above it. The number on the left is the rank the
    order was computed from, which weighs the kind against the age, so it is not
    the classifier's confidence. The statement wraps, so it reads as a sentence
    rather than as a fragment, and it is cut at two lines so one long statement
    cannot push the panel below it off the screen.
    """
    if not rows:
        body = Text("nothing yet. say something worth remembering.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=False, padding=(0, 1))
        table.add_column("", width=5, justify="right", style="dim")
        table.add_column("statement", overflow="fold", ratio=1)
        now = datetime.now(UTC)
        for row in rows:
            colour = KIND_STYLE.get(row["kind"], "white")
            where = row["scope_key"] or "everywhere"
            # A statement scoped above this one is reached by inheritance, and
            # saying so is the difference between a list of what is here and a
            # list of everything the turn was handed.
            inherited = scope is not None and where not in ("everywhere", scope)
            said_by = row.get("actor") or "nobody recorded"
            table.add_row(
                f"{row['rank']:.2f}",
                Text(row["kind"], style=colour)
                + Text(f"  {where}{' \u2191' if inherited else ''}", style="dim")
                + Text(f"  {ago(row['created_at'], now)}", style="dim")
                + Text(f"  by {said_by}", style="dim")
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


def ago(when: str | None, now: datetime) -> str:
    """How long ago a moment was, in as few characters as it takes."""
    if not when:
        return ""
    seconds = int((now - datetime.fromisoformat(when)).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def written_panel(rows: list[dict]) -> Panel:
    """The statements the writer has just produced, newest first.

    A statement here can rank below everything in the panel above it and never
    appear there, which is the point of this one: a plan or an approval is worth
    watching arrive even when it is not worth reading yet.
    """
    if not rows:
        body = Text("nothing written yet.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=False, padding=(0, 1))
        table.add_column("when", width=6, style="dim", no_wrap=True, justify="right")
        table.add_column("what", ratio=1, no_wrap=True, overflow="ellipsis")
        now = datetime.now(UTC)
        for row in rows:
            colour = KIND_STYLE.get(row["kind"], "white")
            where = row["scope_key"] or "everywhere"
            table.add_row(
                ago(row["created_at"], now),
                Text(row["kind"], style=colour)
                + Text(f"  {row['rank']:.2f}  {where}  ", style="dim")
                + Text(" ".join(row["statement"].split())),
            )
        body = table

    return Panel(
        body,
        title="[bold]just written[/bold]",
        subtitle=f"[dim]{len(rows)} statement{'s' if len(rows) != 1 else ''}[/dim]",
        border_style="magenta",
    )


def readings_panel(rows: list[dict]) -> Panel:
    """What the classifier read most recently, newest first.

    A message that cleared no threshold is here and nowhere else, which is how a
    reading that produced no memory is still seen.
    """
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
        subtitle=f"[dim]{len(rows)} reading{'s' if len(rows) != 1 else ''}[/dim]",
        border_style="blue",
    )


# What the three panels cost in rows besides the rows of statements: two borders
# each and one line for the status.
PANEL_CHROME = 7

# A top-of-mind row is a header line and a statement cut to two, and a long kind
# or scope can push it to three, so the fit is planned against three. Planning
# high costs a row of statements, and planning low costs a panel.
TOP_OF_MIND_ROW = 3


def fitting(args) -> tuple[int, int, int]:
    """How many statements each panel draws, given the rows the terminal has.

    A panel that runs off the bottom of the terminal is the panel nobody reads,
    so every count from the command line is an upper bound. The list at the top
    gives way first and the two feeds at the bottom give way last, because the
    feeds are what a person is watching.
    """
    room = console.height - PANEL_CHROME
    limit, new, read = args.limit, args.new, args.read
    while limit > 1 and TOP_OF_MIND_ROW * limit + new + read > room:
        limit -= 1
    while new > 1 and TOP_OF_MIND_ROW * limit + new + read > room:
        new -= 1
    while read > 1 and TOP_OF_MIND_ROW * limit + new + read > room:
        read -= 1
    return limit, new, read


def status(args, counts: tuple[int, int, int]) -> str:
    """What is drawn, and what the terminal had no room for."""
    named = (
        ("top of mind", args.limit, counts[0]),
        ("just written", args.new, counts[1]),
        ("just read", args.read, counts[2]),
    )
    drawn = " · ".join(
        f"{drew} {name}" if drew == asked else f"{drew} of {asked} {name}"
        for name, asked, drew in named
    )
    return f"  {drawn} · polling {args.endpoint}"


def frame(args, state: dict) -> Group:
    """One redraw, from whatever the last poll returned, fitted to the terminal."""
    if state.get("error"):
        return Group(
            Panel(Text(state["error"], style="red"), title="top of mind", border_style="red"),
            Panel(Text(""), title="just written"),
            Panel(Text(""), title="just read"),
        )

    limit, new, read = fitting(args)
    return Group(
        memories_panel(state.get("memories", [])[:limit], args.scope),
        written_panel(state.get("written", [])[:new]),
        readings_panel(state.get("readings", [])[:read]),
        Text(status(args, (limit, new, read)), style="dim"),
    )


def poll(args, state: dict) -> None:
    """Get the three lists, and remember how they failed rather than raising."""
    try:
        state["memories"] = fetch(
            args.endpoint, "/memories", scope_key=args.scope, limit=args.limit, order="rank"
        )
        state["written"] = fetch(
            args.endpoint, "/memories", scope_key=args.scope, limit=args.new, order="newest"
        )
        state["readings"] = fetch(args.endpoint, "/classifications", above=0.0, limit=args.read)
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
    parser.add_argument("--limit", type=int, default=10, help="how many top-of-mind statements")
    parser.add_argument("--new", type=int, default=10, help="how many recently written statements")
    parser.add_argument("--read", type=int, default=10, help="how many recent readings")
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
