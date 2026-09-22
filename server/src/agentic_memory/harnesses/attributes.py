"""OTLP attributes, and the resource and scope a parsed transcript carries.

`agentic_memory.otlp` decodes an export that arrived. This builds the one the
readers produce, so the two directions stay apart.
"""

from typing import Any

SCOPE = "agentic-memory.backfill"
VERSION = "0.0.1"


def attribute(key: str, value: Any) -> dict[str, Any] | None:
    """One OTLP attribute, or nothing when the value is missing."""
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def kept(*attributes: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Drop the attributes a caller could not fill."""
    return [found for found in attributes if found is not None]


def nanos(milliseconds: float) -> str:
    """Milliseconds since the epoch, as OTLP carries them."""
    return str(int(milliseconds) * 1_000_000)


def resource(machine: str) -> dict[str, Any]:
    """The resource attributes, which describe the machine the session ran on.

    A client that reads its own transcripts also reports that machine's
    architecture and operating system. The service cannot: it has the
    machine's name and nothing else, and reporting its own would name the
    wrong machine, which is worse than naming none.
    """
    return {
        "attributes": kept(
            attribute("service.name", "agentic-memory"),
            attribute("service.version", VERSION),
            attribute("service.instance.id", machine),
            attribute("host.name", machine),
            attribute("telemetry.sdk.name", "agentic-memory"),
            attribute("telemetry.sdk.language", "python"),
            attribute("telemetry.sdk.version", VERSION),
        )
    }
