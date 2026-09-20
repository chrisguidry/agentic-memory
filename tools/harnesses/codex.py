"""Codex's sessions.

One JSONL file per rollout, named `rollout-<timestamp>-<rollout id>.jsonl`
under a directory per day. Every line is a flat envelope with a `type` and a
`payload`, and the payload has its own `type`, so the pair decides what a line
is.

Three things about this format are worth knowing before reading the code.

**A conversation is many files.** `session_meta` carries both a `session_id`
and an `id`. The `session_id` is the conversation, and the `id` is the rollout
that file holds. One conversation can span twenty rollouts, each a separate
agent thread, so the session is the conversation and the rollout names the
thread a record came from.

**A line carries a sequence number, and it is per rollout.** `ordinal` counts
from zero for the life of the file, so it orders records that share a
timestamp. It does not identify a line on its own, because every rollout
starts again at zero.

**The harness injects messages as a `developer` role.** They are the harness's
own instructions rather than anything a person said, so they are recorded as
the system actor.
"""

import json
from pathlib import Path
from typing import Any, Iterator

from otlp import attribute, kept
from records import Session

name = "codex"
DEFAULT_SOURCE = Path.home() / ".codex" / "sessions"

# The payload types that are the harness's own state rather than conversation.
BOOKKEEPING = {
    "task_started",
    "task_complete",
    "item_completed",
    "thread_settings_applied",
    "world_state",
    "state",
    "inter_agent_communication_metadata",
}


def discover(source: Path) -> list[Path]:
    return sorted(source.rglob("*.jsonl"))


def read(path: Path, machine: str) -> Iterator[dict[str, Any]]:
    session: Session | None = None
    rollout: str | None = None

    for index, entry in enumerate(_entries(path)):
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        kind_of = entry.get("type")

        if kind_of == "session_meta":
            # The conversation is the session. The rollout is this file, and
            # it is part of a record's identifier because a conversation
            # spans many of them.
            rollout = payload.get("id")
            session = Session(
                harness=name,
                session_id=payload.get("session_id") or payload.get("id"),
                cwd=payload.get("cwd"),
                machine=machine,
                version=payload.get("cli_version"),
            )
            continue
        if session is None:
            continue
        if kind_of == "turn_context":
            session.moved_to(payload.get("cwd"))
            continue

        yield from _entry(entry, payload, session, rollout, index, path.stem)


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


def _identity(
    entry: dict[str, Any],
    payload: dict[str, Any],
    rollout: str | None,
    index: int,
    file_id: str,
) -> str:
    """The harness's identifier for a line, or an address that stands in.

    Most lines carry a payload id. Some carry only an ordinal, which is unique
    within a rollout and repeats across the rollouts of one conversation, so
    the rollout is part of the address. The rest carry neither, and the file
    and the place in it stand in. Content is not used, because a harness
    repeats a line word for word when nothing has changed.
    """
    found = payload.get("id")
    if isinstance(found, str) and found:
        return found
    ordinal = entry.get("ordinal")
    if ordinal is not None:
        return f"{rollout or file_id}:{ordinal}"
    return f"{rollout or file_id}:line-{index}"


def _when(entry: dict[str, Any]) -> float | None:
    stamp = entry.get("timestamp")
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if isinstance(stamp, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def _joined(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and "text" in block
    )


def _entry(
    entry: dict[str, Any],
    payload: dict[str, Any],
    session: Session,
    rollout: str | None,
    index: int,
    file_id: str,
) -> Iterator[dict[str, Any]]:
    kind_of = entry.get("type")
    payload_kind = payload.get("type")
    entry_id = _identity(entry, payload, rollout, index, file_id)
    when = _when(entry)

    if kind_of == "response_item":
        if payload_kind == "message":
            role = payload.get("role")
            if role == "assistant":
                yield session.record(
                    entry=entry_id,
                    kind="response",
                    actor="agent",
                    body=_joined(payload),
                    when_ms=when,
                    extra=[
                        attribute("gen_ai.operation.name", "chat"),
                        attribute("agentic_memory.content", json.dumps(payload)),
                    ],
                )
            elif role in ("developer", "system"):
                yield session.record(
                    entry=entry_id,
                    kind="system",
                    actor="system",
                    body=_joined(payload),
                    when_ms=when,
                )
            else:
                yield session.record(
                    entry=entry_id,
                    kind="prompt",
                    actor="human",
                    body=_joined(payload),
                    when_ms=when,
                    extra=[attribute("agentic_memory.content", json.dumps(payload))],
                )

        elif payload_kind == "reasoning":
            summary = payload.get("summary")
            body = "\n".join(
                str(block.get("text", ""))
                for block in (summary if isinstance(summary, list) else [])
                if isinstance(block, dict)
            )
            yield session.record(
                entry=entry_id, kind="thinking", actor="agent", body=body, when_ms=when
            )

        elif payload_kind in ("custom_tool_call", "function_call"):
            yield session.record(
                entry=entry_id,
                kind="tool_call",
                actor="agent",
                body=str(payload.get("input") or payload.get("arguments") or ""),
                when_ms=when,
                extra=[
                    attribute("gen_ai.operation.name", "execute_tool"),
                    attribute("gen_ai.tool.name", payload.get("name")),
                    attribute("gen_ai.tool.call.id", payload.get("call_id")),
                    attribute("gen_ai.tool.type", "function"),
                ],
            )

        elif payload_kind in ("custom_tool_call_output", "function_call_output"):
            output = payload.get("output")
            body = (
                output
                if isinstance(output, str)
                else "\n".join(
                    str(block.get("text", ""))
                    for block in (output if isinstance(output, list) else [])
                    if isinstance(block, dict)
                )
            )
            yield session.record(
                entry=entry_id,
                kind="tool_result",
                actor="agent",
                body=body,
                when_ms=when,
                extra=[
                    attribute("gen_ai.operation.name", "execute_tool"),
                    attribute("gen_ai.tool.call.id", payload.get("call_id")),
                    attribute("agentic_memory.content", json.dumps(payload)),
                ],
            )

        elif payload_kind == "token_count":
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
            yield session.record(
                entry=entry_id,
                kind="usage",
                actor="system",
                body=json.dumps(info),
                when_ms=when,
                extra=[
                    attribute("gen_ai.usage.input_tokens", usage.get("input_tokens")),
                    attribute("gen_ai.usage.output_tokens", usage.get("output_tokens")),
                    attribute(
                        "gen_ai.usage.cache_read.input_tokens", usage.get("cached_input_tokens")
                    ),
                    attribute(
                        "gen_ai.usage.cache_write.input_tokens",
                        usage.get("cache_write_input_tokens"),
                    ),
                    attribute(
                        "gen_ai.usage.reasoning.output_tokens",
                        usage.get("reasoning_output_tokens"),
                    ),
                ],
            )

        else:
            yield _bookkeeping(entry_id, payload_kind, payload, session, when)

    elif kind_of == "event_msg":
        if payload_kind == "user_message":
            yield session.record(
                entry=entry_id,
                kind="prompt",
                actor="human",
                body=str(payload.get("message", "")),
                when_ms=when,
            )
        elif payload_kind == "agent_message":
            yield session.record(
                entry=entry_id,
                kind="response",
                actor="agent",
                body=str(payload.get("message", "")),
                when_ms=when,
                extra=[attribute("agentic_memory.phase", payload.get("phase"))],
            )
        else:
            yield _bookkeeping(entry_id, payload_kind, payload, session, when)

    else:
        yield _bookkeeping(entry_id, payload_kind, payload, session, when)


def _bookkeeping(
    entry_id: str,
    payload_kind: Any,
    payload: dict[str, Any],
    session: Session,
    when: float | None,
) -> dict[str, Any]:
    """A line the harness wrote for itself."""
    return session.record(
        entry=entry_id,
        kind="bookkeeping",
        actor="system",
        body=json.dumps(payload),
        when_ms=when,
        extra=kept(attribute("agentic_memory.bookkeeping.type", payload_kind)),
    )
