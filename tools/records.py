"""The shape of a record, and the parts of it every harness shares.

A harness reads its own format and hands the pieces to a `Session`, which
builds the record. Nothing in this module reads a harness format, and nothing
in a harness module builds the common attributes by hand.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from otlp import attribute, kept, nanos
from scope import scope_of


def session_id_in(path: str | None) -> str | None:
    """The session id inside a session file path.

    A harness that reports a parent as the path of the session file it forked
    from is giving a path where the convention wants an identifier, so the id
    is taken out of the path.
    """
    if not path:
        return None
    name = Path(path).name
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    return name.rsplit("_", 1)[-1]


def remote_parts(url: str | None) -> tuple[str | None, str | None]:
    """The owner and the repository name inside a git remote URL.

    Both shapes appear: `git@host:owner/name.git` and `https://host/owner/name`.
    A URL with no owner gives the name alone.
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


class Repository:
    """The git repository a directory belongs to, read once per directory."""

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


class Session:
    """The attributes that are the same on every record from one session."""

    def __init__(
        self,
        *,
        harness: str,
        session_id: str | None,
        cwd: str | None,
        machine: str,
        previous: str | None = None,
        root: str = "person",
        depth: int = 0,
        version: str | None = None,
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.machine = machine
        self.cwd = cwd
        self.previous = previous
        self.root = root
        self.depth = depth
        self.version = version
        self.scope, self.scope_kind = scope_of(cwd) if cwd else (None, None)
        self._repository = Repository()

    def moved_to(self, cwd: str | None) -> None:
        """Record that the session moved to another working directory.

        A harness can report a working directory per turn, and the scope
        follows it, because the scope is derived from where the work is.
        """
        if cwd and cwd != self.cwd:
            self.cwd = cwd
            self.scope, self.scope_kind = scope_of(cwd)

    def shared(self) -> list[dict[str, Any]]:
        """What is the same on every record from this session."""
        return kept(
            attribute("session.id", self.session_id),
            attribute("session.previous_id", self.previous),
            attribute("gen_ai.conversation.id", self.session_id),
            attribute("gen_ai.agent.name", self.harness),
            attribute("gen_ai.agent.version", self.version),
            attribute("agentic_memory.scope", self.scope),
            attribute("agentic_memory.scope.kind", self.scope_kind),
            attribute("agentic_memory.root", self.root),
            attribute("agentic_memory.actor.depth", self.depth),
            attribute("process.working_directory", self.cwd),
        ) + self._repository.of(self.cwd)

    def record(
        self,
        *,
        entry: str | None,
        kind: str,
        actor: str,
        body: str,
        when_ms: float | None,
        within: str | None = None,
        extra: list[dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """One log record.

        `entry` is the harness's own identifier for the thing this came from.
        The kind is appended because one entry can produce more than one
        record, and two producers must derive the same identifier for the same
        thing or a re-send is stored twice.
        """
        identity = ":".join(part for part in (entry, kind, within) if part) or None

        return {
            "timeUnixNano": nanos(when_ms) if when_ms is not None else nanos(0),
            "severityText": "INFO",
            "body": {"stringValue": body},
            "attributes": self.shared()
            + kept(
                attribute("agentic_memory.entry.id", identity),
                attribute("agentic_memory.actor", actor),
                attribute("agentic_memory.kind", kind),
            )
            + kept(*(extra or [])),
        }
