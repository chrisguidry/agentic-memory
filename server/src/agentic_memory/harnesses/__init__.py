"""One module per harness, and the registry that names them.

Adding a harness is one module in this package and one line in `HARNESSES`.
A module answers one question: what records the lines of one of its session
files hold. The client sends the lines and never reads a format.
"""

from collections.abc import Iterator
from typing import Any, Protocol

from . import claude_code, codex, pi
from .records import Transcript

__all__ = ["HARNESSES", "Harness", "Transcript"]


class Harness(Protocol):
    """What a harness module provides."""

    name: str

    # Whether lines from the middle of a file can be read on their own. A
    # format whose first line carries the session cannot, so the service
    # refuses a chunk of one rather than storing records that name no session.
    reads_from_the_middle: bool

    def read(self, transcript: Transcript) -> Iterator[dict[str, Any]]:
        """Every record the transcript's lines hold."""


HARNESSES: dict[str, Harness] = {module.name: module for module in (pi, claude_code, codex)}
