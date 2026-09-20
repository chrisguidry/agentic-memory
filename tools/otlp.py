"""The wire: OTLP attributes, a resource, and the export call.

Nothing here knows about a harness. A harness builds records with the
helpers in `records.py` and this module puts them on the wire.
"""

import json
import platform
import urllib.request
from pathlib import Path
from socket import gethostname
from typing import Any

DEFAULT_ENDPOINT = "http://127.0.0.1:4318/v1/logs"
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


def machine_id() -> str | None:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(path).read_text().strip()
        except OSError:
            continue
    return None


def resource(machine: str) -> dict[str, Any]:
    """The resource attributes, which describe the machine rather than a record."""
    return {
        "attributes": kept(
            attribute("service.name", "agentic-memory"),
            attribute("service.version", VERSION),
            attribute("service.instance.id", machine),
            attribute("host.name", machine),
            attribute("host.id", machine_id()),
            attribute("host.arch", platform.machine()),
            attribute("os.type", platform.system().lower()),
            attribute("os.version", platform.release()),
            attribute("telemetry.sdk.name", "agentic-memory"),
            attribute("telemetry.sdk.language", "python"),
            attribute("telemetry.sdk.version", VERSION),
        )
    }


def post(endpoint: str, machine: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Send one batch, and report what the service did with it."""
    payload = {
        "resourceLogs": [
            {
                "resource": resource(machine),
                "scopeLogs": [
                    {"scope": {"name": SCOPE, "version": VERSION}, "logRecords": records}
                ],
            }
        ]
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as answer:
        return json.loads(answer.read())


def default_machine() -> str:
    return gethostname()
