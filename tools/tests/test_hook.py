"""Shipping a Claude Code transcript from a hook, a chunk at a time.

The service here is a real HTTP server on a free port that keeps what it was
sent, because what the hook does is post to one, and a fake would prove only
that the hook called a function.
"""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import hook
import pytest
from harnesses import claude_code

SESSION = "11111111-2222-3333-4444-555555555555"
CWD = "/home/someone/src/example.test/acme/widget"
MACHINE = "laptop.example.test"


class Service:
    """A service that remembers every record it was sent."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        received = self.records

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                batch = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
                received.extend(batch)
                answer = json.dumps({"inserted": len(batch), "repeated": 0, "failed": 0})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(answer.encode())

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/logs"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def entry_ids(self) -> list[str]:
        return [
            pair["value"]["stringValue"]
            for record in self.records
            for pair in record["attributes"]
            if pair["key"] == "agentic_memory.entry.id"
        ]


@pytest.fixture
def service() -> Iterator[Service]:
    found = Service()
    yield found
    found.server.shutdown()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / f"{SESSION}.jsonl"


def entry(kind: str, text: str, *, uuid: str) -> dict[str, Any]:
    return {
        "type": kind,
        "uuid": uuid,
        "sessionId": SESSION,
        "cwd": CWD,
        "timestamp": "2026-09-21T12:00:00.000Z",
        "message": {"role": kind, "content": text},
    }


def bookkeeping(kind: str) -> dict[str, Any]:
    """An entry with no id of its own, which the reader names by its place."""
    return {"type": kind, "sessionId": SESSION, "value": kind}


def line(found: dict[str, Any]) -> str:
    return json.dumps(found) + "\n"


def exchange() -> str:
    return line(entry("user", "hello", uuid="u1")) + line(entry("assistant", "hi", uuid="a1"))


def ship(transcript: Path, service: Service, state_dir: Path) -> int:
    return hook.ship(transcript, endpoint=service.endpoint, state_dir=state_dir, machine=MACHINE)


class TestShipping:
    def test_a_new_transcript_is_sent_whole(self, transcript, service, state_dir):
        transcript.write_text(exchange())
        assert ship(transcript, service, state_dir) == 2
        assert service.entry_ids() == ["u1:prompt", "a1:response"]

    def test_a_second_event_sends_only_what_was_added(self, transcript, service, state_dir):
        transcript.write_text(line(entry("user", "hello", uuid="u1")))
        ship(transcript, service, state_dir)
        with transcript.open("a") as appending:
            appending.write(line(entry("assistant", "hi", uuid="a1")))
        assert ship(transcript, service, state_dir) == 1
        assert service.entry_ids() == ["u1:prompt", "a1:response"]

    def test_nothing_new_sends_nothing(self, transcript, service, state_dir):
        transcript.write_text(line(entry("user", "hello", uuid="u1")))
        ship(transcript, service, state_dir)
        assert ship(transcript, service, state_dir) == 0
        assert len(service.records) == 1

    def test_a_line_still_being_written_waits(self, transcript, service, state_dir):
        reply = line(entry("assistant", "hi", uuid="a1"))
        transcript.write_text(line(entry("user", "hello", uuid="u1")) + reply[:20])
        assert ship(transcript, service, state_dir) == 1
        with transcript.open("a") as appending:
            appending.write(reply[20:])
        assert ship(transcript, service, state_dir) == 1
        assert service.entry_ids() == ["u1:prompt", "a1:response"]

    def test_a_rewritten_transcript_starts_over(self, transcript, service, state_dir):
        transcript.write_text(exchange())
        ship(transcript, service, state_dir)
        transcript.write_text(line(entry("user", "again", uuid="u2")))
        assert ship(transcript, service, state_dir) == 1
        assert service.entry_ids()[-1] == "u2:prompt"

    def test_a_missing_transcript_sends_nothing(self, transcript, service, state_dir):
        assert ship(transcript, service, state_dir) == 0
        assert service.records == []


class TestPlaces:
    """An entry with no id is named by its place in the file, and the place must
    be the same whether the file was read in chunks or at once."""

    def test_a_chunk_names_entries_as_the_whole_file_would(self, transcript, service, state_dir):
        first = line(entry("user", "hello", uuid="u1")) + line(bookkeeping("mode"))
        second = line(bookkeeping("last-prompt")) + line(entry("assistant", "hi", uuid="a1"))
        transcript.write_text(first)
        ship(transcript, service, state_dir)
        transcript.write_text(first + second)
        ship(transcript, service, state_dir)

        whole = [
            pair["value"]["stringValue"]
            for record in claude_code.read(transcript, MACHINE)
            for pair in record["attributes"]
            if pair["key"] == "agentic_memory.entry.id"
        ]
        assert service.entry_ids() == whole

    def test_a_chunk_with_no_working_directory_keeps_the_sessions(
        self, transcript, service, state_dir
    ):
        transcript.write_text(line(entry("user", "hello", uuid="u1")))
        ship(transcript, service, state_dir)
        transcript.write_text(line(entry("user", "hello", uuid="u1")) + line(bookkeeping("mode")))
        ship(transcript, service, state_dir)
        scopes = [
            pair["value"]["stringValue"]
            for pair in service.records[-1]["attributes"]
            if pair["key"] == "process.working_directory"
        ]
        assert scopes == [CWD]

    def test_a_first_chunk_with_no_working_directory_waits(self, transcript, service, state_dir):
        transcript.write_text(line(bookkeeping("mode")))
        assert ship(transcript, service, state_dir) == 0
        transcript.write_text(line(bookkeeping("mode")) + line(entry("user", "hello", uuid="u1")))
        assert ship(transcript, service, state_dir) == 2


HOOK = Path(hook.__file__)


def run_hook(stdin: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", **env},
    )


class TestForeground:
    def test_a_bad_payload_exits_zero_and_says_nothing(self):
        done = run_hook("this is not json")
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")

    def test_a_payload_with_no_transcript_exits_zero_and_says_nothing(self):
        done = run_hook(json.dumps({"hook_event_name": "Stop"}))
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")

    def test_the_turn_does_not_wait_for_the_send(self, transcript, service, state_dir, tmp_path):
        transcript.write_text(line(entry("user", "hello", uuid="u1")))
        payload = json.dumps({"transcript_path": str(transcript), "cwd": CWD})

        started = time.monotonic()
        done = run_hook(
            payload, XDG_STATE_HOME=str(tmp_path), AGENTIC_MEMORY_ENDPOINT=service.endpoint
        )
        foreground = time.monotonic() - started

        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
        assert foreground < 1.0
        deadline = time.monotonic() + 5
        while not service.records and time.monotonic() < deadline:
            time.sleep(0.05)
        assert service.entry_ids() == ["u1:prompt"]
