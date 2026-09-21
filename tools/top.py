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
would actually get.

    uv run tools/top.py
    uv run tools/top.py --scope github.com/chrisguidry/agentic-memory
    uv run tools/top.py --endpoint http://127.0.0.1:4318 --every 1
"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

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
    """What is worth remembering, ranked the way a turn would read it."""
    if not rows:
        body = Text("nothing yet. say something worth remembering.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=True)
        table.add_column("", width=4, justify="right", style="dim")
        table.add_column("kind", width=13)
        table.add_column("statement", overflow="fold")
        for row in rows:
            colour = KIND_STYLE.get(row["kind"], "white")
            where = row["scope_key"] or "everywhere"
            table.add_row(
                f"{row['score']:.2f}",
                Text(row["kind"], style=colour),
                Text(row["statement"]) + Text(f"\n{where}", style="dim"),
            )
        body = table

    return Panel(
        body,
        title="[bold]top of mind[/bold]"
        + (f" [dim]{scope}[/dim]" if scope else " [dim]all scopes[/dim]"),
        subtitle=f"[dim]{len(rows)} statement{'s' if len(rows) != 1 else ''}[/dim]",
        border_style="green",
    )


def readings_panel(rows: list[dict]) -> Panel:
    """What the classifier read most recently, highest first."""
    if not rows:
        body = Text("nothing read yet.", style="dim")
    else:
        table = Table(box=None, pad_edge=False, expand=True, show_header=False)
        table.add_column("when", width=8, style="dim")
        table.add_column("what", overflow="fold")
        for row in rows[:12]:
            when = (row["classified_at"] or "")[11:19]
            best = max(
                (row[kind] for kind in KIND_STYLE if kind in row),
                default=0.0,
            )
            winner = max(
                (kind for kind in KIND_STYLE if kind in row),
                key=lambda kind: row[kind],
                default="",
            )
            message = " ".join((row["message"] or "").split())[:110]
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
        readings_panel(state.get("readings", [])),
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
        state["readings"] = fetch(args.endpoint, "/classifications", above=0.0, limit=30)
        state["read"] = len(state["readings"])
        state["written"] = len(state["memories"])
        state["error"] = None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as problem:
        state["error"] = f"cannot reach the service at {args.endpoint}: {problem}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:4318")
    parser.add_argument("--scope", default=None, help="a scope to read from, or all of them")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--every", type=float, default=2.0, help="seconds between polls")
    args = parser.parse_args()

    state: dict = {}
    with Live(frame(args, state), console=console, refresh_per_second=4) as live:
        while True:
            poll(args, state)
            live.update(frame(args, state))
            time.sleep(args.every)


if __name__ == "__main__":
    main()
