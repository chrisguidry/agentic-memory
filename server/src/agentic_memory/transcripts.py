"""Turning a client's transcript lines into an OTLP export.

A client sends the whole lines one of its transcript files gained, and the
service reads them with the harness's own reader. The export this builds is
the one a client that read the file itself would have posted to `/v1/logs`,
so the same entry arrives with the same identifier down either path and the
store keeps one of it.
"""

from typing import Any

from pydantic import BaseModel, Field

from .harnesses import HARNESSES, Transcript
from .harnesses.attributes import SCOPE, VERSION, resource
from .harnesses.records import Repository


class Chunk(BaseModel):
    """What a client says about the lines it is sending."""

    harness: str
    machine: str
    path: str
    lines: list[str]
    cwd: str | None = None
    version: str | None = None
    scope: str | None = None
    scope_kind: str | None = None
    # The git repository the working directory belongs to. Only the machine
    # that holds the checkout can read it.
    repository: Repository | None = None
    # How many entries of this file the client has already sent. A reader
    # numbers an entry that carries no id of its own by its place in the file,
    # so a chunk read from zero would name those entries wrongly and the store
    # would hold each of them twice.
    entries_before: int = Field(0, ge=0)


def export(chunk: Chunk) -> dict[str, Any]:
    """The OTLP export the chunk's lines hold.

    Raises `ValueError` when the harness is not one the service reads, or when
    the lines are from the middle of a file whose format cannot be read that
    way.
    """
    harness = HARNESSES.get(chunk.harness)
    if harness is None:
        raise ValueError(
            f"{chunk.harness} is not a harness this service reads: {', '.join(sorted(HARNESSES))}"
        )
    if chunk.entries_before and not harness.reads_from_the_middle:
        raise ValueError(
            f"a {chunk.harness} file is read from its first entry, "
            f"and these lines follow {chunk.entries_before}"
        )

    records = list(
        harness.read(
            Transcript(
                machine=chunk.machine,
                path=chunk.path,
                lines=chunk.lines,
                cwd=chunk.cwd,
                version=chunk.version,
                scope=chunk.scope,
                scope_kind=chunk.scope_kind,
                repository=chunk.repository,
                entries_before=chunk.entries_before,
            )
        )
    )
    return {
        "resourceLogs": [
            {
                "resource": resource(chunk.machine),
                "scopeLogs": [
                    {"scope": {"name": SCOPE, "version": VERSION}, "logRecords": records}
                ],
            }
        ]
    }
