"""pi's sessions.

One JSONL file per session, named `<timestamp>_<session id>.jsonl`, inside a
directory named for the working directory it belongs to. The first line is
the session header, and every line after it is an entry in a tree.

An assistant message has text blocks, thinking blocks, and tool calls, so
one entry can produce more than one record. The entry id and the kind together
name each record, which is the same identifier `pi/index.ts` derives, so a
session both paths captured is stored once.
"""

import json
from collections.abc import Iterator
from typing import Any

from .attributes import attribute
from .records import Session, Transcript, entries, session_id_in

name = "pi"

# The first line is the session header, and every record needs it, so a chunk
# that starts after it cannot be read on its own.
reads_from_the_middle = False


def read(transcript: Transcript) -> Iterator[dict[str, Any]]:
    session: Session | None = None

    for entry in entries(transcript.lines):
        if entry.get("type") == "session":
            session = Session(
                harness=name,
                session_id=entry.get("id"),
                cwd=entry.get("cwd") or transcript.cwd,
                machine=transcript.machine,
                scope=transcript.scope,
                scope_kind=transcript.scope_kind,
                repository=transcript.repository,
                previous=session_id_in(entry.get("parentSession")),
            )
            continue
        if session is None or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if isinstance(message, dict):
            yield from _message(entry, message, session)


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": "" if content is None else str(content)}]


def _joined(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")


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
        # out, and a later pass reads it apart from the answer.
        for index, block in enumerate(found for found in blocks if found.get("type") == "thinking"):
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

        # A tool call is its own record, so a call and its result join on the call
        # id. pi puts the call in the assistant message.
        for block in (found for found in blocks if found.get("type") == "toolCall"):
            yield session.record(
                entry=entry_id,
                kind="tool_call",
                actor="agent",
                body=json.dumps(block.get("arguments", {})),
                when_ms=when,
                within=str(block.get("id")),
                extra=[
                    attribute("gen_ai.operation.name", "execute_tool"),
                    attribute("gen_ai.tool.name", block.get("name")),
                    attribute("gen_ai.tool.call.id", block.get("id")),
                    attribute("gen_ai.tool.type", "function"),
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
