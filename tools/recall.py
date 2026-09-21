#!/usr/bin/env python3
"""Hand a Claude Code turn what this person's earlier sessions said.

Claude Code runs this on `UserPromptSubmit` with a JSON payload on stdin. The
hook derives the scope from the working directory, asks the service for the
statements worth reading there, and writes them to stdout as the context the
hook docs specify, so the model sees them on this turn.

Two rules shape everything here.

**The deadline is ours.** Claude Code blocks the turn until this hook returns,
so the service has 150 milliseconds in all, and past that the turn proceeds
with nothing. A missing service, a slow one, and an empty answer all look the
same from the turn: no memory this time.

**A failure never reaches the model.** Nothing but the block is ever written
to stdout, because on this event stdout is context. Everything else goes to a
log under the state directory.

Register it in `~/.claude/settings.json` under `UserPromptSubmit`:

    {"type": "command",
     "command": "python3 -S /path/to/agentic-memory/tools/recall.py",
     "timeout": 5}

`-S` and the absence of `logging` and `urllib` here are deliberate. Each of
those costs the interpreter tens of milliseconds to import, and the whole
budget is 150. The request is a few lines over a socket instead.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Measured from the first line of the program rather than from the request, so
# the interpreter's own start counts against the budget.
STARTED = time.monotonic()
DEADLINE = 0.150

DEFAULT_ENDPOINT = "http://127.0.0.1:4318/recall"
LOG = "recall.log"
DEFAULT_LIMIT = 10
HEADING = "Statements this person's earlier sessions produced, with where each came from:"

DEFAULT_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
STATE_DIR = DEFAULT_STATE / "agentic-memory" / "claude-code"


def main() -> int:
    """Read the payload, ask, and print the block. Always exit zero."""
    try:
        payload = json.load(sys.stdin)
        endpoint = os.environ.get("AGENTIC_MEMORY_ENDPOINT", DEFAULT_ENDPOINT)
        limit = int(os.environ.get("AGENTIC_MEMORY_RECALL_LIMIT", DEFAULT_LIMIT))
        block = recall(
            payload, endpoint=endpoint, limit=limit, state_dir=STATE_DIR, started=STARTED
        )
        if block:
            # The shape the hook docs give for adding context on this event. A
            # plain line on stdout is also taken as context, and the envelope
            # is used so that nothing about the block is left to a parser.
            envelope = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": block,
                }
            }
            sys.stdout.write(json.dumps(envelope))
    except Exception:
        pass
    return 0


def recall(
    payload: dict[str, Any],
    *,
    endpoint: str,
    limit: int,
    state_dir: Path,
    home: Path | None = None,
    deadline: float = DEADLINE,
    started: float | None = None,
    now: datetime | None = None,
) -> str | None:
    """The block for this turn, or nothing when there is nothing to say.

    `started` is when the clock on the deadline began. The hook passes the
    moment the interpreter reached its first line, so its own start counts.
    """
    if started is None:
        started = time.monotonic()
    log = Log(state_dir)
    try:
        moment = now or datetime.now(UTC)
        return _recall(payload, endpoint, limit, home, deadline, started, moment, log)
    except Exception as failure:
        log.note(f"could not recall for {payload.get('session_id')}: {failure!r}")
        return None


def _recall(
    payload: dict[str, Any],
    endpoint: str,
    limit: int,
    home: Path | None,
    deadline: float,
    started: float,
    now: datetime,
    log: Log,
) -> str | None:
    # Imported here because it runs git, and the import is time the turn waits.
    from scope import scope_of

    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    if not cwd or not session_id:
        log.note("no working directory or session in the payload")
        return None

    scope_key, _ = scope_of(Path(cwd), home)
    asked = {
        "session_id": session_id,
        "harness": "claude-code",
        "scope_key": scope_key,
        "limit": limit,
    }

    # Whatever the interpreter and the scope took is already spent.
    budget = deadline - (time.monotonic() - started)
    if budget <= 0:
        log.note(f"{session_id}: the deadline passed before the service was asked")
        return None

    try:
        statements = post(endpoint, asked, budget).get("statements", [])
    except (OSError, ValueError) as failure:
        log.note(f"{session_id}: no answer within {budget * 1000:.0f} ms: {failure!r}")
        return None

    if not statements:
        return None
    log.note(f"{session_id}: {len(statements)} statements for {scope_key}")
    return block(statements, now)


def post(endpoint: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One HTTP POST over a socket, with the whole exchange under one timeout.

    `urllib` would do this in one line and cost more to import than the
    service takes to answer. The service is on the loopback and answers in one
    small JSON body, so the request needs nothing a socket does not have.
    """
    if not endpoint.startswith("http://"):
        raise ValueError(f"only http is spoken here, not {endpoint}")
    host_and_port, _, path = endpoint[len("http://") :].partition("/")
    host, _, port = host_and_port.partition(":")
    payload = json.dumps(body).encode()
    request = (
        f"POST /{path} HTTP/1.1\r\nHost: {host_and_port}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode() + payload

    deadline = time.monotonic() + timeout
    with socket.create_connection((host, int(port or 80)), timeout=timeout) as connection:
        connection.sendall(request)
        received = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the deadline passed while reading the answer")
            connection.settimeout(remaining)
            chunk = connection.recv(65536)
            if not chunk:
                break
            received.extend(chunk)

    head, _, rest = bytes(received).partition(b"\r\n\r\n")
    status = head.split(b" ", 2)
    if len(status) < 2 or status[1] != b"200":
        raise ValueError(f"the service answered {head.splitlines()[0]!r}")
    return json.loads(rest)


def block(statements: list[dict[str, Any]], now: datetime) -> str:
    """The statements as the model reads them, one line each, with provenance.

    The statement is given as the writer wrote it. Nothing here is filtered or
    rewritten, because the point of the first injection is to watch the raw
    list land.
    """
    return "\n".join([HEADING, *(line(found, now) for found in statements)])


def line(found: dict[str, Any], now: datetime) -> str:
    where = found.get("scope_key") or "everywhere"
    return f"- {found.get('kind')}, {where}, {who(found)}, {age(found, now)}: {found['statement']}"


def who(found: dict[str, Any]) -> str:
    depth = found.get("actor_depth") or 0
    if found.get("actor") == "human" and depth == 0:
        return "person"
    return f"{found.get('actor') or 'unknown'} at depth {depth}"


def age(found: dict[str, Any], now: datetime) -> str:
    said_at = found.get("said_at")
    if not said_at:
        return "undated"
    when = datetime.fromisoformat(said_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    days = max((now - when).days, 0)
    return "today" if days == 0 else f"{days} day{'' if days == 1 else 's'} ago"


class Log:
    """One line per event, appended to a file under the state directory."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / LOG

    def note(self, message: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as out:
                out.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
