"""Reading an OTLP logs export.

The wire format is OTLP/HTTP with the JSON encoding. A log record arrives
inside three envelopes, and this module walks them and turns each record
into a row the service can store.

The attribute names it reads are listed in `docs/otel-conventions.md`.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any


def value(encoded: dict[str, Any]) -> Any:
    """Decode one OTLP attribute value.

    OTLP encodes a value as a one-key object whose key names the type. A key
    this function does not know means a producer sent something the service
    does not read, and the value is dropped rather than guessed at. A value
    that is not an object at all is not OTLP, and it is dropped the same way.
    """
    if not isinstance(encoded, dict):
        return None
    for key, reader in (
        ("stringValue", str),
        ("intValue", int),
        ("doubleValue", float),
        ("boolValue", bool),
    ):
        if key in encoded:
            return reader(encoded[key])
    if "arrayValue" in encoded:
        return [value(item) for item in encoded["arrayValue"].get("values", [])]
    if "kvlistValue" in encoded:
        items = encoded["kvlistValue"].get("values", [])
        return {item["key"]: value(item["value"]) for item in items}
    return None


def attributes(pairs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Turn OTLP's list of key and value pairs into a plain mapping."""
    return {pair["key"]: value(pair["value"]) for pair in pairs or []}


def clean(value: Any) -> Any:
    """Replace the character PostgreSQL refuses to store.

    A transcript can carry a NUL byte, because a tool read a binary file or
    a terminal emitted one. PostgreSQL rejects U+0000 in both `text` and
    `jsonb`, so it becomes U+FFFD, which is Unicode's own way of saying a
    character could not be represented. The alternative is a record that
    cannot be stored at all.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def walk(payload: dict[str, Any]) -> Iterator[tuple[dict, dict, dict]]:
    """Yield one `(resource, scope, record)` for each log record in an export."""
    for resource_logs in payload.get("resourceLogs") or []:
        resource = attributes((resource_logs.get("resource") or {}).get("attributes"))
        for scope_logs in resource_logs.get("scopeLogs") or []:
            scope = scope_logs.get("scope") or {}
            for record in scope_logs.get("logRecords") or []:
                yield clean(resource), clean(scope), clean(record)


def _moment(nanos: Any) -> datetime | None:
    """Turn OTLP's nanoseconds since the epoch into a timestamp."""
    if nanos is None:
        return None
    try:
        return datetime.fromtimestamp(int(nanos) / 1_000_000_000, tz=UTC)
    except TypeError, ValueError:
        return None


def _body(record: dict[str, Any]) -> str | None:
    """The record's text, whatever shape the producer sent it in.

    A body that does not decode becomes SQL null rather than a placeholder,
    because the envelope holds the original and a caller can tell the two
    apart. Writing a placeholder here would hide the difference.
    """
    if record.get("body") is None:
        return None
    decoded = value(record["body"])
    if decoded is None:
        return None
    if isinstance(decoded, str):
        return decoded
    return json.dumps(decoded, ensure_ascii=False)


def _text(attribute: str, record: dict, resource: dict) -> str | None:
    found = record.get(attribute, resource.get(attribute))
    return None if found is None else str(found)


def _whole(attribute: str, record: dict, resource: dict) -> int | None:
    found = record.get(attribute, resource.get(attribute))
    try:
        return None if found is None else int(found)
    except TypeError, ValueError:
        return None


def _fraction(attribute: str, record: dict, resource: dict) -> float | None:
    found = record.get(attribute, resource.get(attribute))
    try:
        return None if found is None else float(found)
    except TypeError, ValueError:
        return None


def row(resource: dict, scope: dict, record: dict) -> dict[str, Any]:
    """Turn one OTLP log record into a row, keeping the envelope whole."""
    carried = attributes(record.get("attributes"))

    return {
        "resource": json.dumps(resource, ensure_ascii=False),
        "scope": json.dumps(scope, ensure_ascii=False),
        "record": json.dumps(record, ensure_ascii=False),
        "occurred_at": _moment(record.get("timeUnixNano")),
        "observed_at": _moment(record.get("observedTimeUnixNano")),
        "severity": record.get("severityText"),
        "body": _body(record),
        "session_id": _text("session.id", carried, resource),
        "previous_session": _text("session.previous_id", carried, resource),
        "entry_id": _text("agentic_memory.entry.id", carried, resource),
        "harness": _text("gen_ai.agent.name", carried, resource),
        "machine": _text("host.name", carried, resource),
        "actor": _text("agentic_memory.actor", carried, resource),
        "actor_depth": _whole("agentic_memory.actor.depth", carried, resource),
        "root": _text("agentic_memory.root", carried, resource),
        "kind": _text("agentic_memory.kind", carried, resource),
        "operation": _text("gen_ai.operation.name", carried, resource),
        "provider": _text("gen_ai.provider.name", carried, resource),
        "model": _text("gen_ai.request.model", carried, resource),
        "response_model": _text("gen_ai.response.model", carried, resource),
        "tool_name": _text("gen_ai.tool.name", carried, resource),
        # The repository is the project when there is one. An organization
        # root holds a dozen repositories, and naming all of them after the
        # root hides which one the work was in.
        "project": _text("vcs.repository.name", carried, resource)
        or _text("agentic_memory.project", carried, resource),
        "repository": _text("vcs.repository.url.full", carried, resource),
        "owner": _text("vcs.owner.name", carried, resource),
        "revision": _text("vcs.ref.head.revision", carried, resource),
        "working_directory": _text("process.working_directory", carried, resource),
        "input_tokens": _whole("gen_ai.usage.input_tokens", carried, resource),
        "output_tokens": _whole("gen_ai.usage.output_tokens", carried, resource),
        "cache_read_tokens": _whole("gen_ai.usage.cache_read.input_tokens", carried, resource),
        "cache_write_tokens": _whole("gen_ai.usage.cache_write.input_tokens", carried, resource),
        "reasoning_tokens": _whole("gen_ai.usage.reasoning.output_tokens", carried, resource),
        "cost_total": _fraction("agentic_memory.cost.total", carried, resource),
        "error_type": _text("error.type", carried, resource),
        "trace_id": record.get("traceId"),
        "span_id": record.get("spanId"),
        "attributes": json.dumps(carried, ensure_ascii=False),
    }
