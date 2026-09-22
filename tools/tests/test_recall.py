"""Handing a turn what earlier sessions said, from a Claude Code hook.

The service here is a real HTTP server on a free port, because the thing being
proved is the deadline: a slow answer is no answer, and the turn goes on.
"""

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import recall

SESSION = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def statement(text: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "statement": text,
        "kind": "preference",
        "scope_key": "example.test/acme/widget",
        "said_at": ago(3),
        "actor": "human",
        "actor_depth": 0,
        **fields,
    }


class Service:
    """A service that answers with what it was given, after a pause if asked."""

    def __init__(self, statements: list[dict[str, Any]], delay: float = 0.0) -> None:
        self.asked: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        asked = self.asked
        authorizations = self.authorizations

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                authorizations.append(self.headers.get("Authorization"))
                time.sleep(delay)
                answer = json.dumps({"statements": statements}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/recall"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A working directory whose scope the hook can derive."""
    home = tmp_path / "home"
    found = home / "src" / "example.test" / "acme" / "widget"
    (found / ".git").mkdir(parents=True)
    return found


def ask(endpoint: str, cwd: Path | None, state_dir: Path, deadline: float = 0.15) -> str | None:
    payload = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return recall.recall(
        payload,
        endpoint=endpoint,
        limit=10,
        state_dir=state_dir,
        home=cwd.parents[4] if cwd is not None else None,
        deadline=deadline,
        now=NOW,
    )


class TestTheBlock:
    def test_one_line_per_statement_with_its_provenance(self, repo, state_dir):
        service = Service(
            [
                statement("Tests come before code here."),
                statement(
                    "Commit messages say why.",
                    id=2,
                    kind="correction",
                    scope_key=None,
                    actor="agent",
                    actor_depth=1,
                    said_at=ago(40),
                ),
            ]
        )
        try:
            block = ask(service.endpoint, repo, state_dir)
        finally:
            service.stop()
        assert block is not None
        lines = block.splitlines()
        assert lines[0] == (
            "Statements from this person's earlier sessions, chosen for this place and this "
            "prompt, with where each came from:"
        )
        assert lines[1] == (
            "- preference, example.test/acme/widget, person, 3 days ago: "
            "Tests come before code here."
        )
        assert lines[2] == (
            "- correction, everywhere, agent at depth 1, 40 days ago: Commit messages say why."
        )

    def test_the_service_is_asked_for_this_session_scope_and_prompt(self, repo, state_dir):
        # The prompt goes with the ask because the service matches statements
        # against it, and the service applies its own limit to its length.
        service = Service([statement("x")])
        try:
            ask(service.endpoint, repo, state_dir)
        finally:
            service.stop()
        assert service.asked == [
            {
                "session_id": SESSION,
                "harness": "claude-code",
                "scope_key": "example.test/acme/widget",
                "prompt": "hi",
                "limit": 10,
            }
        ]

    def test_a_missing_prompt_is_sent_as_empty(self, repo, state_dir):
        service = Service([statement("x")])
        payload = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "cwd": str(repo)}
        try:
            recall.recall(
                payload,
                endpoint=service.endpoint,
                limit=10,
                state_dir=state_dir,
                home=repo.parents[4],
                now=NOW,
            )
        finally:
            service.stop()
        assert service.asked[0]["prompt"] == ""

    def test_an_empty_answer_is_no_block(self, repo, state_dir):
        service = Service([])
        try:
            assert ask(service.endpoint, repo, state_dir) is None
        finally:
            service.stop()


class TestNothingOnFailure:
    def test_a_slow_service_is_no_answer_inside_the_deadline(self, repo, state_dir):
        service = Service([statement("late")], delay=1.0)
        try:
            started = time.monotonic()
            block = ask(service.endpoint, repo, state_dir, deadline=0.15)
            elapsed = time.monotonic() - started
        finally:
            service.stop()
        assert block is None
        assert elapsed < 0.5

    def test_a_service_that_is_down_is_no_answer(self, repo, state_dir):
        service = Service([statement("x")])
        service.stop()
        service.server.server_close()
        assert ask(service.endpoint, repo, state_dir) is None

    def test_no_working_directory_is_no_answer(self, state_dir):
        service = Service([statement("x")])
        try:
            assert ask(service.endpoint, None, state_dir) is None
        finally:
            service.stop()
        assert service.asked == []


class TestTheEnvelope:
    def test_stdout_is_the_shape_the_hook_docs_specify(self, repo, state_dir):
        service = Service([statement("Tests come before code here.")])
        payload = json.dumps(
            {"session_id": SESSION, "cwd": str(repo), "hook_event_name": "UserPromptSubmit"}
        )
        try:
            done = subprocess.run(
                [sys.executable, "-S", str(Path(recall.__file__))],
                input=payload,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    "AGENTIC_MEMORY_ENDPOINT": service.endpoint,
                    "HOME": str(repo.parents[4]),
                    "XDG_STATE_HOME": str(state_dir),
                    "PATH": "/usr/bin:/bin",
                },
            )
        finally:
            service.stop()
        assert done.returncode == 0
        assert done.stderr == ""
        envelope = json.loads(done.stdout)
        assert set(envelope) == {"hookSpecificOutput"}
        assert envelope["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "Tests come before code here." in envelope["hookSpecificOutput"]["additionalContext"]

    def test_a_bad_payload_is_silent_and_exits_zero(self, state_dir):
        done = subprocess.run(
            [sys.executable, "-S", str(Path(recall.__file__))],
            input="not json",
            capture_output=True,
            text=True,
            timeout=10,
            env={"XDG_STATE_HOME": str(state_dir), "PATH": "/usr/bin:/bin"},
        )
        assert done.returncode == 0
        assert done.stdout == ""
        assert done.stderr == ""


class TestTheAuthorizationHeader:
    """A deployment behind a proxy asks for one header, and the hook sends it."""

    def test_it_is_sent_when_the_environment_names_one(self, repo, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIC_MEMORY_AUTHORIZATION", "Basic Y2hyaXM6c2VjcmV0")
        service = Service([statement("x")])
        try:
            ask(service.endpoint, repo, state_dir)
        finally:
            service.stop()
        assert service.authorizations == ["Basic Y2hyaXM6c2VjcmV0"]

    def test_it_is_absent_when_the_environment_names_none(self, repo, state_dir, monkeypatch):
        monkeypatch.delenv("AGENTIC_MEMORY_AUTHORIZATION", raising=False)
        service = Service([statement("x")])
        try:
            ask(service.endpoint, repo, state_dir)
        finally:
            service.stop()
        assert service.authorizations == [None]
