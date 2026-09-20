"""pi's sessions.

One JSONL file per session, named `<timestamp>_<session id>.jsonl`, inside a
directory named for the working directory it belongs to. The first line is
the session header, and every line after it is an entry in a tree.

An assistant message carries text blocks, thinking blocks, and tool calls, so
one entry can produce more than one record. The entry id and the kind together
name each record, which is the same identifier `pi/index.ts` derives, so a
session both paths captured is stored once.
"""

import json
from pathlib import Path
from typing import Any, Iterator

from otlp import attribute, kept
from records import Session, session_id_in

name = "pi"
DEFAULT_SOURCE = Path.home() / ".pi" / "agent" / "sessions"


def discover(source: Path) -> list[Path]:
    return sorted(source.rglob("*.jsonl"))


def read(path: Path, machine: str) -> Iterator[dict[str, Any]]:
    session: Session | None = None

    for entry in _entries(path):
        if entry.get("type") == "session":
            session = Session(
                harness=name,
                session_id=entry.get("id"),
                cwd=entry.get("cwd"),
                machine=machine,
                previous=session_id_in(entry.get("parentSession")),
            )
            continue
        if session is None or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if isinstance(message, dict):
            yield from _message(entry, message, session)


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
            # A line that does not parse is a partial write. The live hook
            # reads the file again later; a backfill can only skip it.
            continue
        if isinstance(found, dict):
            yield found


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": "" if content is None else str(content)}]


def _joined(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(block.get("text", "")) for block in blocks if block.get("type") == "text"
    )


def _message(
    entry: dict[str, Any], message: dict[str, Any], session: Session
) -> Iterator[dict[str, Any]]:
    entry_id = entry.get("id")
    when = message.get("timestamp")
    blocks = _blocks(message)
    role = message.get("role")

    if role == "user":
        yield session.record(
            entry=entry_id, kind="prompt", actor="human", body=_joined(blocks), when_ms=when
        )

    elif role == "assistant":
        usage = message.get("usage") or {}
        cost = usage.get("cost") or {}
        yield session.record(
            entry=entry_id,
            kind="response",
            actor="agent",
            body=_joined(blocks),
            when_ms=when,
            extra=[
                attribute("gen_ai.operation.name", "chat"),
                attribute("gen_ai.provider.name", message.get("provider")),
                attribute("gen_ai.request.model", message.get("model")),
                attribute(
                    "gen_ai.response.model", message.get("responseModel") or message.get("model")
                ),
                attribute("gen_ai.response.id", message.get("responseId")),
                attribute("gen_ai.response.finish_reasons", message.get("stopReason")),
                attribute("gen_ai.request.reasoning.level", message.get("providerThinkingLevel")),
                attribute("gen_ai.usage.input_tokens", usage.get("input")),
                attribute("gen_ai.usage.output_tokens", usage.get("output")),
                attribute("gen_ai.usage.cache_read.input_tokens", usage.get("cacheRead")),
                attribute("gen_ai.usage.cache_write.input_tokens", usage.get("cacheWrite")),
                attribute("gen_ai.usage.reasoning.output_tokens", usage.get("reasoning")),
                attribute("agentic_memory.cost.total", cost.get("total")),
                attribute("agentic_memory.content", json.dumps(blocks)),
            ],
        )
        # Reasoning is its own record. It is the part other harnesses leave
        # out, and a later pass wants to read it apart from the answer.
        for index, block in enumerate(
            found for found in blocks if found.get("type") == "thinking"
        ):
            yield session.record(
                entry=entry_id,
                kind="thinking",
                actor="agent",
                body=str(block.get("thinking", "")),
                when_ms=when,
                within=str(index),
                extra=[
                    attribute("gen_ai.operation.name", "chat"),
                    attribute("gen_ai.request.model", message.get("model")),
                ],
            )

    elif role == "toolResult":
        yield session.record(
            entry=entry_id,
            kind="tool_result",
            actor="agent",
            body=_joined(blocks),
            when_ms=when,
            extra=[
                attribute("gen_ai.operation.name", "execute_tool"),
                attribute("gen_ai.tool.name", message.get("toolName")),
                attribute("gen_ai.tool.call.id", message.get("toolCallId")),
                attribute("gen_ai.tool.type", "function"),
                attribute("agentic_memory.content", json.dumps(blocks)),
                attribute("error.type", "tool_error") if message.get("isError") else None,
            ],
        )

    elif role == "system":
        sections = message.get("sections")
        yield session.record(
            entry=entry_id,
            kind="system",
            actor="system",
            body=json.dumps(sections if sections is not None else message.get("content", "")),
            when_ms=when,
        )
