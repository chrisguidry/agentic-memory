#!/usr/bin/env python3
"""Ship a Claude Code session to the service as it happens.

Claude Code runs this on `UserPromptSubmit`, `Stop`, `SubagentStop`, and
`SessionEnd`, with a JSON payload on stdin that names the session's transcript
file. The hook reads what the transcript gained since the last event and sends
it as OTLP log records, through the same reader the backfill uses, so a record
sent here and the same record swept later carry the same entry id and the
service keeps one.

Two rules shape everything here.

**The turn never waits.** The foreground reads the payload, hands it to a
detached child, and exits with status zero before the child has done anything.
A missing server, a bad line, or a vanished file is written to a log under the
state directory and never to stdout or stderr, because Claude Code shows a
hook's stderr and adds a `UserPromptSubmit` hook's stdout to the conversation.

**The offset is the only state.** For each transcript the hook keeps how many
bytes and how many entries it has shipped, and a fingerprint of the file's
head. A file that shrinks or whose head changes was rewritten, and the hook
starts it over from zero. The service drops the repeats, so starting over costs
bandwidth and never a duplicate.

Register it in `~/.claude/settings.json`:

    {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 /path/to/agentic-memory/tools/hook.py", "timeout": 5}]}]}}

and the same entry under `Stop`, `SubagentStop`, and `SessionEnd`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# XDG puts state that should survive a restart but that nobody would miss if it
# were lost under `~/.local/state`. Losing an offset means re-sending a file the
# service already holds, which is exactly that kind of loss.
DEFAULT_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
STATE_DIR = DEFAULT_STATE / "agentic-memory" / "claude-code"
BATCH = 200

# How much of the file's head the fingerprint covers. A rewrite that keeps the
# first four kilobytes byte for byte and is no shorter than the offset is not
# caught, and the service drops what it re-sends.
HEAD = 4096

log = logging.getLogger("agentic_memory.hook")


def main() -> int:
    """Read the payload and hand it to a detached child. Always exit zero."""
    try:
        payload = json.load(sys.stdin)
        path = payload.get("transcript_path")
        if not path:
            return 0
        # The endpoint is read here rather than defaulted in `ship`, so a test
        # and a machine with the service elsewhere can name it without a flag.
        endpoint = os.environ.get("AGENTIC_MEMORY_ENDPOINT")
        detach(lambda: ship(Path(path), cwd=payload.get("cwd"), endpoint=endpoint))
    except Exception:
        pass
    return 0


def detach(work) -> None:
    """Run `work` in a process Claude Code is not waiting for.

    Claude Code waits for the hook's stdout to close, not only for the process
    to exit, so the child closes every inherited descriptor before it starts.
    The second fork gives the child to init, so nothing is left to reap.
    """
    if os.fork():
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.close(devnull)
        work()
    except BaseException:
        pass
    finally:
        os._exit(0)


def ship(
    path: Path,
    *,
    cwd: str | None = None,
    endpoint: str | None = None,
    state_dir: Path = STATE_DIR,
    machine: str | None = None,
) -> int:
    """Send what the transcript gained since the last time, and return how many records."""
    state_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(state_dir)
    try:
        return _ship(path, cwd, endpoint, state_dir, machine)
    except Exception:
        log.exception("could not ship %s", path)
        return 0


def _ship(
    path: Path, cwd: str | None, endpoint: str | None, state_dir: Path, machine: str | None
) -> int:
    # The imports are here because the foreground never needs them, and every
    # module the foreground loads is time the turn spends waiting.
    from harnesses import claude_code
    from otlp import DEFAULT_ENDPOINT, default_machine, post

    endpoint = endpoint or DEFAULT_ENDPOINT
    machine = machine or default_machine()

    with Offset(state_dir, path) as offset:
        chunk = offset.unread()
        if not chunk:
            return 0
        lines = chunk.decode(errors="replace").splitlines()
        entries = [line for line in lines if line.strip()]

        # The reader needs the session's working directory to derive a scope.
        # It takes it from the first entry that has one, and a chunk from the
        # middle of a file may have none, so the offset remembers it.
        found_cwd = first(entries, "cwd") or offset.cwd or cwd
        found_version = first(entries, "version") or offset.version
        if found_cwd is None:
            log.info("%s: waiting for an entry with a working directory", path.name)
            return 0

        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / path.name
            partial.write_bytes(padding(offset.entries, found_cwd, found_version) + chunk)
            records = list(claude_code.read(partial, machine))

        sent = 0
        for start in range(0, len(records), BATCH):
            answer = post(endpoint, machine, records[start : start + BATCH])
            sent += answer.get("inserted", 0) + answer.get("repeated", 0)
        offset.advance(len(chunk), len(parsed(entries)), found_cwd, found_version)
        log.info("%s: sent %d records, %d new", path.name, len(records), sent)
        return len(records)


def first(lines: list[str], key: str) -> str | None:
    """The value of `key` on the first entry that carries it."""
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get(key):
            return str(entry[key])
    return None


def parsed(lines: list[str]) -> list[dict[str, Any]]:
    """The entries the reader will count, by the reader's own rule."""
    found = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def padding(entries: int, cwd: str, version: str | None) -> bytes:
    """Stand-ins for the entries already shipped, so the reader sees the same file.

    An entry with no id of its own is named by its place in the file. The reader
    counts places over the whole file, so a chunk fed to it on its own would
    name those entries wrongly, and the sweep would store each one a second
    time. An empty object takes a place and yields no record. The first one
    also carries the working directory, which the reader takes from the first
    entry that has it.
    """
    if entries == 0:
        return b""
    head = json.dumps({"cwd": cwd, "version": version}) + "\n"
    return (head + "{}\n" * (entries - 1)).encode()


class Offset:
    """Where the hook has read a transcript to, held under a lock while it reads.

    The lock is on the state file, so two events on one session, which Claude
    Code fires close together, ship the file in turn rather than both from the
    same offset.
    """

    def __init__(self, state_dir: Path, path: Path) -> None:
        self.path = path
        self.file = state_dir / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + ".json")
        self.offset = 0
        self.entries = 0
        self.head = ""
        self.cwd: str | None = None
        self.version: str | None = None

    def __enter__(self) -> Offset:
        self.handle = open(self.file, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        self.handle.seek(0)
        text = self.handle.read()
        if text:
            saved = json.loads(text)
            self.offset = saved.get("offset", 0)
            self.entries = saved.get("entries", 0)
            self.head = saved.get("head", "")
            self.cwd = saved.get("cwd")
            self.version = saved.get("version")
        return self

    def __exit__(self, *exc) -> None:
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()

    def unread(self) -> bytes:
        """The whole lines after the offset, or the whole file when it was rewritten."""
        try:
            with open(self.path, "rb") as transcript:
                transcript.seek(0, os.SEEK_END)
                size = transcript.tell()
                if self.offset and (
                    size < self.offset or fingerprint(transcript, self.offset) != self.head
                ):
                    log.info("%s: rewritten, starting over", self.path.name)
                    self.offset, self.entries = 0, 0
                transcript.seek(self.offset)
                chunk = transcript.read()
        except OSError:
            log.info("%s: cannot be read", self.path.name)
            return b""

        # A line still being written ends without a newline, and a partial entry
        # is not an entry. It is read on the next event.
        complete = chunk.rfind(b"\n")
        return chunk[: complete + 1] if complete >= 0 else b""

    def advance(self, size: int, entries: int, cwd: str | None, version: str | None) -> None:
        self.offset += size
        self.entries += entries
        self.cwd = cwd
        self.version = version
        with open(self.path, "rb") as transcript:
            self.head = fingerprint(transcript, self.offset)
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(
            {
                "path": str(self.path),
                "offset": self.offset,
                "entries": self.entries,
                "head": self.head,
                "cwd": self.cwd,
                "version": self.version,
            },
            self.handle,
        )
        self.handle.flush()


def fingerprint(transcript, offset: int) -> str:
    """A hash of the head of the file, over bytes the hook has already shipped.

    Only shipped bytes can be compared, because the file grows between events
    and a hash over more than the offset would change every time.
    """
    transcript.seek(0)
    return hashlib.sha256(transcript.read(min(HEAD, offset))).hexdigest()


def configure_logging(state_dir: Path) -> None:
    if log.handlers:
        return
    handler = logging.FileHandler(state_dir / "hook.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


if __name__ == "__main__":
    raise SystemExit(main())
