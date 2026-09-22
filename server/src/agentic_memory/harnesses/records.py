"""The shape of a record, and the parts of it every harness shares.

A harness reads its own format and hands the pieces to a `Session`, which
builds the record. Nothing in this module reads a harness format, and nothing
in a harness module builds the common attributes by hand.
"""

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from .attributes import attribute, kept, nanos


class Repository(BaseModel):
    """The git repository the session's working directory belongs to.

    Only the machine that holds the checkout can read these, so the client
    reads them and sends them. Every field is optional: a directory that is
    not a repository has none of them, and a repository with no remote has no
    owner or URL.
    """

    name: str | None = None
    owner: str | None = None
    url: str | None = None
    branch: str | None = None
    revision: str | None = None

    def attributes(self) -> list[dict[str, Any]]:
        return kept(
            attribute("vcs.repository.name", self.name),
            attribute("vcs.owner.name", self.owner),
            attribute("vcs.repository.url.full", self.url),
            attribute("vcs.ref.head.name", self.branch),
            attribute("vcs.ref.head.revision", self.revision),
        )


@dataclass(frozen=True)
class Transcript:
    """Lines from one transcript file, and what the client says about it.

    The client sends whole lines from an offset. `entries_before` is how many
    entries came before them, so a reader numbers an entry the way a read of
    the whole file would. `cwd` and `version` are the first the client ever saw
    for this file, because a chunk from the middle may carry neither, and
    `scope` is derived on the machine that holds the directories.
    """

    machine: str
    path: str
    lines: list[str]
    cwd: str | None = None
    version: str | None = None
    scope: str | None = None
    scope_kind: str | None = None
    repository: Repository | None = None
    entries_before: int = 0

    @property
    def file_id(self) -> str:
        """The file's name without its suffix, which names entries that have no id."""
        return PurePosixPath(self.path).stem


def session_id_in(path: str | None) -> str | None:
    """The session id inside a session file path.

    A harness that reports a parent as the path of the session file it forked
    from is giving a path where the convention wants an identifier, so the id
    is taken out of the path.
    """
    if not path:
        return None
    name = PurePosixPath(path).name
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    return name.rsplit("_", 1)[-1]


class Session:
    """The attributes that are the same on every record from one session."""

    def __init__(
        self,
        *,
        harness: str,
        session_id: str | None,
        cwd: str | None,
        machine: str,
        scope: str | None = None,
        scope_kind: str | None = None,
        repository: Repository | None = None,
        previous: str | None = None,
        root: str = "person",
        depth: int = 0,
        version: str | None = None,
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.machine = machine
        self.cwd = cwd
        self.scope = scope
        self.scope_kind = scope_kind
        self.repository = repository
        self.previous = previous
        self.root = root
        self.depth = depth
        self.version = version

    def moved_to(self, cwd: str | None) -> None:
        """Record that the session moved to another working directory.

        The scope stays where it was. Only the machine that holds the
        directories can derive a scope from one, and it sent the scope it
        derived from the first directory it saw for this file.
        """
        if cwd:
            self.cwd = cwd

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
        ) + (self.repository.attributes() if self.repository else [])

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

        found = {
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

        # A record that carries no time of its own says so. Sending zero
        # instead would date it to 1970 and sort it before everything real.
        if when_ms is not None:
            found["timeUnixNano"] = nanos(when_ms)

        return found


def entries(lines: list[str]) -> list[dict[str, Any]]:
    """Every JSON object among the lines.

    A line that does not parse is a partial write. The client reads the file
    again later and sends the line whole, so skipping it costs nothing.
    """
    found = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
    return found
