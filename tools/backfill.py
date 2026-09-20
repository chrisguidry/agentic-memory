#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Load the sessions pi already wrote into the service.

This is a proof of concept. It reads the session files pi left on disk and
sends them as OTLP log records, so the store has history to work from
before the live capture has run for long.

It builds each record the same way `pi/index.ts` does, including the entry
identifier, so a session that both paths captured is stored once.

    uv run tools/backfill.py --from ~/.pi/agent/sessions
    uv run tools/backfill.py --machine desktop --from /mnt/old-home/.pi/agent/sessions
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from socket import gethostname
from typing import Any

from scope import scope_of

DEFAULT_ENDPOINT = "http://127.0.0.1:4318/v1/logs"
SCOPE = "agentic-memory.pi"
VERSION = "0.0.1"
BATCH = 200


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
    return [found for found in attributes if found is not None]


def nanos(milliseconds: float) -> str:
    return str(int(milliseconds) * 1_000_000)


def session_id_in(path: str | None) -> str | None:
    """The session id inside a session file path.

    pi reports a parent as the path of the session file it forked from. The
    convention wants the previous `session.id`, so the id is taken out of the
    path rather than the path being sent as an identifier.
    """
    if not path:
        return None
    name = Path(path).name
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    return name.rsplit("_", 1)[-1]


def machine_id() -> str | None:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(path).read_text().strip()
        except OSError:
            continue
    return None


def resource(machine: str) -> dict[str, Any]:
    """The resource attributes, which describe the machine rather than a record."""
    import platform

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


class Repository:
    """The git repository a working directory belongs to, read once per directory."""

    def __init__(self) -> None:
        self._seen: dict[str, list[dict[str, Any]]] = {}

    def of(self, cwd: str | None) -> list[dict[str, Any]]:
        if cwd is None:
            return []
        if cwd not in self._seen:
            root = self._git(cwd, "rev-parse", "--show-toplevel")
            remote = self._git(cwd, "config", "--get", "remote.origin.url")
            owner, named = remote_parts(remote)
            self._seen[cwd] = kept(
                attribute("vcs.repository.name", Path(root).name if root else named),
                attribute("vcs.owner.name", owner),
                attribute("vcs.repository.url.full", remote),
                attribute("vcs.ref.head.name", self._git(cwd, "rev-parse", "--abbrev-ref", "HEAD")),
                attribute("vcs.ref.head.revision", self._git(cwd, "rev-parse", "HEAD")),
            )
        return self._seen[cwd]

    @staticmethod
    def _git(cwd: str, *args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() or None if done.returncode == 0 else None


def remote_parts(url: str | None) -> tuple[str | None, str | None]:
    """The owner and the repository name inside a git remote URL.

    Both shapes appear: `git@host:owner/name.git` and
    `https://host/owner/name`. A URL with no owner gives the name alone.
    """
    if not url:
        return None, None
    if "://" in url:
        path = url.split("://", 1)[1]
    elif "@" in url and ":" in url:
        path = url.split(":", 1)[1]
    else:
        path = url
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None, None
    named = segments[-1].removesuffix(".git")
    return (segments[-2] if len(segments) >= 2 else None), named


def blocks_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": "" if content is None else str(content)}]


def joined(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(block.get("text", "")) for block in blocks if block.get("type") == "text"
    )


def records_for(
    entry: dict[str, Any],
    header: dict[str, Any],
    machine: str,
    repository: Repository,
) -> Iterator[dict[str, Any]]:
    """The records one session entry deserves."""
    message = entry.get("message")
    if entry.get("type") != "message" or not isinstance(message, dict):
        return

    cwd = header.get("cwd")
    session = header.get("id")
    scope, scope_kind = scope_of(Path(cwd)) if cwd else (None, None)
    common = kept(
        attribute("session.id", session),
        attribute("session.previous_id", session_id_in(header.get("parentSession"))),
        attribute("gen_ai.conversation.id", session),
        attribute("gen_ai.agent.name", "pi"),
        attribute("agentic_memory.scope", scope),
        attribute("agentic_memory.scope.kind", scope_kind),
        attribute("agentic_memory.root", "person"),
        attribute("agentic_memory.actor.depth", 0),
        attribute("process.working_directory", cwd),
    ) + repository.of(cwd)

    when = nanos(message.get("timestamp") or 0)
    entry_id = entry.get("id")
    role = message.get("role")
    blocks = blocks_of(message)

    def build(
        kind: str,
        actor: str,
        body: str,
        extra: list | None = None,
        within: str | None = None,
    ) -> dict:
        identity = ":".join(part for part in (entry_id, kind, within) if part)
        return {
            "timeUnixNano": when,
            "severityText": "INFO",
            "body": {"stringValue": body},
            "attributes": common
            + kept(
                attribute("agentic_memory.entry.id", identity),
                attribute("agentic_memory.actor", actor),
                attribute("agentic_memory.kind", kind),
            )
            + kept(*(extra or [])),
        }

    if role == "user":
        yield build("prompt", "human", joined(blocks), [])

    elif role == "assistant":
        usage = message.get("usage") or {}
        cost = usage.get("cost") or {}
        yield build(
            "response",
            "agent",
            joined(blocks),
            [
                attribute("gen_ai.operation.name", "chat"),
                attribute("gen_ai.provider.name", message.get("provider")),
                attribute("gen_ai.request.model", message.get("model")),
                attribute("gen_ai.response.model", message.get("responseModel") or message.get("model")),
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
        for index, block in enumerate(b for b in blocks if b.get("type") == "thinking"):
            yield build(
                "thinking",
                "agent",
                str(block.get("thinking", "")),
                [
                    attribute("gen_ai.operation.name", "chat"),
                    attribute("gen_ai.request.model", message.get("model")),
                ],
                within=str(index),
            )

    elif role == "toolResult":
        yield build(
            "tool_result",
            "agent",
            joined(blocks),
            [
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
        yield build("system", "system", json.dumps(sections if sections is not None else message.get("content", "")))


def read_session(path: Path, machine: str, repository: Repository) -> Iterator[dict[str, Any]]:
    """Every record in one session file."""
    header: dict[str, Any] | None = None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as problem:
        print(f"  skipped {path}: {problem}", file=sys.stderr)
        return

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A line that does not parse is a partial write. The live path
            # reads the file again later; a backfill can only skip it.
            continue
        if entry.get("type") == "session":
            header = entry
            continue
        if header is None:
            continue
        yield from records_for(entry, header, machine, repository)


def post(endpoint: str, machine: str, records: list[dict[str, Any]]) -> dict[str, Any]:
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
    with urllib.request.urlopen(request, timeout=60) as answer:
        return json.loads(answer.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=Path.home() / ".pi" / "agent" / "sessions",
        help="a session directory, or a copy of one from another machine",
    )
    parser.add_argument(
        "--machine",
        default=gethostname(),
        help="the machine the transcripts came from, never guessed from this host",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--limit", type=int, help="stop after this many files")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()

    files = sorted(options.source.rglob("*.jsonl"))
    if options.limit:
        files = files[: options.limit]
    if not files:
        print(f"no session files under {options.source}", file=sys.stderr)
        return 1

    print(f"{len(files)} session files under {options.source}")
    print(f"reading them as {options.machine}, sending to {options.endpoint}")

    repository = Repository()
    batch: list[dict[str, Any]] = []
    inserted = repeated = failed = 0
    started = datetime.now(UTC)

    def send() -> None:
        nonlocal inserted, repeated, failed
        if options.dry_run or not batch:
            batch.clear()
            return
        answer = post(options.endpoint, options.machine, batch)
        inserted += answer.get("inserted", 0)
        repeated += answer.get("repeated", 0)
        failed += answer.get("failed", 0)
        batch.clear()

    for number, path in enumerate(files, start=1):
        for record in read_session(path, options.machine, repository):
            batch.append(record)
            if len(batch) >= options.batch:
                send()
        if number % 50 == 0 or number == len(files):
            elapsed = (datetime.now(UTC) - started).total_seconds()
            print(
                f"  {number}/{len(files)} files, {inserted} new, "
                f"{repeated} repeats, {failed} refused, {elapsed:.0f}s"
            )

    send()

    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(
        f"done in {elapsed:.0f}s: {inserted} new records, "
        f"{repeated} already held, {failed} refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
