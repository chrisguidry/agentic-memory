"""One module per harness, and the registry that names them.

Adding a harness is one module in this package and one line in `HARNESSES`.
A module answers two questions: which files under a source belong to this
harness, and what records one of those files holds.
"""

from pathlib import Path
from typing import Any, Iterator, Protocol

from harnesses import claude_code, codex, pi


class Harness(Protocol):
    """What a harness module provides."""

    name: str
    DEFAULT_SOURCE: Path

    def discover(self, source: Path) -> list[Path]:
        """Every session file this harness left under a source."""

    def read(self, path: Path, machine: str) -> Iterator[dict[str, Any]]:
        """Every record one session file holds."""


HARNESSES: dict[str, Harness] = {
    module.name: module for module in (pi, claude_code, codex)
}
