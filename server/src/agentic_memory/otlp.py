"""Reading an OTLP logs export.

The wire format is OTLP/HTTP with the JSON encoding. A log record arrives
inside three envelopes, and this module walks them and turns each record
into a row the service can store.

The attribute names it reads are listed in `docs/otel-conventions.md`.
"""

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any


def value(encoded: dict[str, Any]) -> Any:
    """Decode one OTLP attribute value.

    OTLP encodes a value as a one-key object whose key names the type. A key
    the service does not recognize is dropped, along with its value, rather
    than guessed at. A value that is not an object at all is not OTLP, and it
    is dropped the same way.
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


def fingerprint(*parts: dict) -> str:
    """A stable name for a map.

    Truncated, because finding an identical map is all it has to do, and the
    unique index that finds it is on this value.
    """
    serialized = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def entry_of(record: dict) -> tuple[Any, Any]:
    """The session and the entry a record came from.

    Two paths that capture the same session read the same entry and derive the
    same value for it, while the context they record around it differs. This
    pair is what deduplicates those two.
    """
    carried = attributes(record.get("attributes"))
    return carried.get("session.id"), carried.get("agentic_memory.entry.id")


def resource_row(resource: dict) -> dict[str, Any]:
    """The row the resources table takes."""
    return {
        "fingerprint": fingerprint(resource),
        "resource": json.dumps(resource, ensure_ascii=False),
    }


def scope_row(scope: dict) -> dict[str, Any]:
    """The row the scopes table takes."""
    return {
        "fingerprint": fingerprint(scope),
        "name": scope.get("name"),
        "version": scope.get("version"),
        "attributes": json.dumps(attributes(scope.get("attributes")), ensure_ascii=False),
    }


def raw(record: dict) -> dict[str, Any]:
    """The row the raw table takes: what arrived, verbatim.

    The resource and the scope are named by reference rather than repeated.
    Both are immutable and both have a table of their own, so a copy here
    would be the third of each. The session and the entry come out of the
    attributes, because they are the key the raw table deduplicates on.
    """
    session_id, entry_id = entry_of(record)
    return {
        "session_id": session_id,
        "entry_id": entry_id,
        "record": json.dumps(record, ensure_ascii=False),
    }


def row(resource: dict, scope: dict, record: dict) -> dict[str, Any]:
    """The row the unpacked table takes.

    The attribute map stays whole beside the fields lifted out of it, so the
    map remains the source of truth and a query does not have to walk one.
    The resource and the scope are left to their own tables.
    """
    carried = attributes(record.get("attributes"))

    return {
        "occurred_at": _moment(record.get("timeUnixNano")),
        "observed_at": _moment(record.get("observedTimeUnixNano")),
        "severity": record.get("severityText"),
        "severity_number": record.get("severityNumber"),
        "trace_id": record.get("traceId"),
        "span_id": record.get("spanId"),
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
        # The scope is derived on the client, from the directories the session
        # is in. The service stores what it is told, because only the machine
        # has the directory layout.
        "scope_key": _text("agentic_memory.scope", carried, resource),
        "scope_kind": _text("agentic_memory.scope.kind", carried, resource),
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
        "attributes": json.dumps(carried, ensure_ascii=False),
    }
