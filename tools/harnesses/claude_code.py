"""Claude Code's sessions.

One JSONL file per session, named for the session's uuid, inside a directory
named for the working directory. There is no header line: every entry has
the session id, the working directory, the branch, and its own uuid.

Two things about this format shape the code that reads it.

**A tool result arrives as a `user` entry.** It is a user-role message whose
content is a `tool_result` block, so an adapter that maps role to actor
records a tool's output as something the person typed. The blocks determine
the actor, not the role.

**Most entries are the harness's own bookkeeping.** `mode`, `last-prompt`,
`atis-latch`, `ai-title`, `queue-operation`, `file-history-*`, `cost-state`,
and a few others carry no conversation at all. Three of them carry things
worth keeping: the session's title, its cost totals, and the files it touched.
They are recorded under their own kinds so a reader can tell them apart.
"""

import json
from pathlib import Path
from typing import Any, Iterator

from otlp import attribute, kept
from records import Session

name = "claude-code"
DEFAULT_SOURCE = Path.home() / ".claude" / "projects"

# The harness's own entries. The three at the top carry something worth
# reading; the rest are state the interface keeps for itself.
KINDS = {
    "ai-title": "title",
    "cost-state": "cost",
    "file-history-delta": "file_history",
    "file-history-snapshot": "file_history",
}
BOOKKEEPING = "bookkeeping"


def discover(source: Path) -> list[Path]:
    return sorted(source.rglob("*.jsonl"))


def read(path: Path, machine: str) -> Iterator[dict[str, Any]]:
    entries = list(_entries(path))

    # There is no header line, and the bookkeeping entries that open a file
    # have a session id but no working directory. The file's working
    # directory is taken from the first entry that has one, so a record that
    # appears before it is still recorded with the session's directory.
    cwd = next((entry.get("cwd") for entry in entries if entry.get("cwd")), None)
    version = next((entry.get("version") for entry in entries if entry.get("version")), None)

    session: Session | None = None
    for index, entry in enumerate(entries):
        if session is None or session.session_id != entry.get("sessionId"):
            # A file can hold more than one session when a session was resumed
            # into it, so the session is rebuilt when the id changes.
            found = _session(entry, machine, cwd, version)
            if found is None:
                continue
            session = found
        session.moved_to(entry.get("cwd"))
        yield from _entry(entry, session, index, path.stem)


def _session(
    entry: dict[str, Any], machine: str, cwd: str | None, version: str | None
) -> Session | None:
    session_id = entry.get("sessionId")
    if not session_id:
        return None
    return Session(
        harness=name,
        session_id=session_id,
        cwd=entry.get("cwd") or cwd,
        machine=machine,
        version=entry.get("version") or version,
        # A sidechain entry belongs to a subagent, which is one agent away
        # from the person who started the session.
        depth=1 if entry.get("isSidechain") else 0,
    )


def _entries(path: Path) -> Iterator[dict[str, Any]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            found = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(found, dict):
            yield found


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": "" if content is None else str(content)}]


def _joined(blocks: list[dict[str, Any]], of: str = "text") -> str:
    return "\n".join(
        str(block.get(of, "")) for block in blocks if block.get("type") == of
    )


def _when(entry: dict[str, Any], message: dict[str, Any]) -> float | None:
    """The entry's timestamp in milliseconds, from whichever shape holds it."""
    stamp = entry.get("timestamp")
    if isinstance(stamp, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    if isinstance(stamp, (int, float)):
        return float(stamp)
    inner = message.get("timestamp")
    return float(inner) if isinstance(inner, (int, float)) else None


def _entry(
    entry: dict[str, Any], session: Session, index: int, file_id: str
) -> Iterator[dict[str, Any]]:
    kind_of = entry.get("type")
    # The bookkeeping entries carry no identifier of their own, and a harness
    # repeats one word for word when nothing has changed, so the file and the
    # place in it stand in. A session can span several files, so the place
    # alone is not enough.
    uuid = entry.get("uuid") or f"{file_id}:line-{index}"
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    when = _when(entry, message)

    # The branch this entry was written on, which git cannot recover later
    # because the session may span several. It overrides the branch the
    # repository is on now.
    branch = kept(attribute("vcs.ref.head.name", entry.get("gitBranch")))

    if kind_of == "user":
        blocks = _blocks(message)
        if any(block.get("type") == "tool_result" for block in blocks):
            yield session.record(
                entry=uuid,
                kind="tool_result",
                actor="agent",
                body=_joined(blocks, "tool_result"),
                when_ms=when,
                extra=branch
                + [
                    attribute("gen_ai.operation.name", "execute_tool"),
                    attribute("agentic_memory.content", json.dumps(blocks)),
                    attribute("error.type", "tool_error")
                    if any(block.get("is_error") for block in blocks)
                    else None,
                ],
            )
        else:
            yield session.record(
                entry=uuid, kind="prompt", actor="human", body=_joined(blocks), when_ms=when,
                extra=branch,
            )

    elif kind_of == "assistant":
        blocks = _blocks(message)
        usage = message.get("usage") or {}
        yield session.record(
            entry=uuid,
            kind="response",
            actor="agent",
            body=_joined(blocks),
            when_ms=when,
            extra=branch
            + [
                attribute("gen_ai.operation.name", "chat"),
                attribute("gen_ai.provider.name", message.get("model") and "anthropic"),
                attribute("gen_ai.request.model", message.get("model")),
                attribute("gen_ai.response.id", message.get("id")),
                attribute("gen_ai.response.finish_reasons", message.get("stop_reason")),
                attribute("gen_ai.usage.input_tokens", usage.get("input_tokens")),
                attribute("gen_ai.usage.output_tokens", usage.get("output_tokens")),
                attribute(
                    "gen_ai.usage.cache_read.input_tokens", usage.get("cache_read_input_tokens")
                ),
                attribute(
                    "gen_ai.usage.cache_write.input_tokens", usage.get("cache_creation_input_tokens")
                ),
                attribute("agentic_memory.content", json.dumps(blocks)),
            ],
        )
        for index, block in enumerate(
            found for found in blocks if found.get("type") == "thinking"
        ):
            yield session.record(
                entry=uuid,
                kind="thinking",
                actor="agent",
                body=str(block.get("thinking", "")),
                when_ms=when,
                within=str(index),
                extra=branch + [attribute("gen_ai.request.model", message.get("model"))],
            )
        for block in (found for found in blocks if found.get("type") == "tool_use"):
            yield session.record(
                entry=uuid,
                kind="tool_call",
                actor="agent",
                body=json.dumps(block.get("input", {})),
                when_ms=when,
                within=str(block.get("id")),
                extra=branch
                + [
                    attribute("gen_ai.operation.name", "execute_tool"),
                    attribute("gen_ai.tool.name", block.get("name")),
                    attribute("gen_ai.tool.call.id", block.get("id")),
                    attribute("gen_ai.tool.type", "function"),
                ],
            )

    elif kind_of == "attachment":
        attachment = entry.get("attachment") or {}
        yield session.record(
            entry=uuid,
            kind="attachment",
            actor="system",
            body=json.dumps(attachment),
            when_ms=when,
            extra=branch
            + [
                attribute("agentic_memory.content", json.dumps(entry)),
                attribute("agentic_memory.attachment.type", attachment.get("type")),
            ],
        )

    elif kind_of == "system":
        yield session.record(
            entry=uuid, kind="system", actor="system", body=json.dumps(message), when_ms=when,
            extra=branch,
        )

    else:
        # The harness's own state. It is kept because it costs almost nothing
        # and three of these carry the title, the cost, and the files touched.
        yield session.record(
            entry=uuid,
            kind=KINDS.get(kind_of, BOOKKEEPING),
            actor="system",
            body=json.dumps(
                {key: value for key, value in entry.items() if key not in ("uuid", "type")}
            ),
            when_ms=when,
            extra=branch
            + [
                attribute("agentic_memory.bookkeeping.type", kind_of),
                attribute("agentic_memory.cost.total", entry.get("totalCostUSD")),
                attribute("agentic_memory.title", entry.get("aiTitle")),
                attribute("agentic_memory.file.path", entry.get("trackingPath")),
            ],
        )
