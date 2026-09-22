"""Reading the lines of a Claude Code session file.

Every fixture here is invented. The shapes are the harness's; the words are not
anyone's.
"""

import json
from typing import Any

import pytest

from agentic_memory.harnesses import Transcript, claude_code

SESSION = "11111111-2222-3333-4444-555555555555"
CWD = "/home/someone/src/example.test/acme/widget"
SCOPE = "example.test/acme/widget"
MACHINE = "a-test-machine"


def entry(kind: str, content: Any, *, uuid: str, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "sessionId": SESSION,
            "cwd": CWD,
            "isSidechain": sidechain,
            "timestamp": "2026-09-21T12:00:00.000Z",
            "message": {"role": kind, "content": content},
        }
    )


def transcript(*lines: str) -> Transcript:
    return Transcript(
        machine=MACHINE,
        path=f"/sessions/{SESSION}.jsonl",
        lines=list(lines),
        cwd=CWD,
        scope=SCOPE,
        scope_kind="repo",
    )


def attributes(record: dict[str, Any]) -> dict[str, Any]:
    found = {}
    for pair in record["attributes"]:
        value = pair["value"]
        found[pair["key"]] = next(iter(value.values()))
    return found


def only(records: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [r for r in records if attributes(r)["agentic_memory.kind"] == kind]
    assert len(found) == 1
    return found[0]


def records_of(found: Transcript) -> list[dict[str, Any]]:
    return list(claude_code.read(found))


@pytest.fixture
def result_as_text() -> Transcript:
    return transcript(
        entry(
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "two files changed"}],
            uuid="u1",
        ),
    )


@pytest.fixture
def result_as_blocks() -> Transcript:
    return transcript(
        entry(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "text", "text": "first line"},
                        {"type": "text", "text": "second line"},
                    ],
                }
            ],
            uuid="u1",
        ),
    )


class TestToolResult:
    def test_a_result_given_as_text_is_the_body(self, result_as_text: Transcript):
        found = only(records_of(result_as_text), "tool_result")
        assert found["body"]["stringValue"] == "two files changed"

    def test_a_result_given_as_blocks_is_joined_into_the_body(self, result_as_blocks: Transcript):
        found = only(records_of(result_as_blocks), "tool_result")
        assert found["body"]["stringValue"] == "first line\nsecond line"

    def test_the_result_is_the_tool_and_not_the_person(self, result_as_text: Transcript):
        found = only(records_of(result_as_text), "tool_result")
        assert attributes(found)["agentic_memory.actor"] == "agent"


@pytest.fixture
def person_prompt() -> Transcript:
    return transcript(entry("user", "please fix the widget", uuid="u1"))


@pytest.fixture
def subagent_prompt() -> Transcript:
    return transcript(
        entry("user", "You are a watch agent for the widget.", uuid="u1", sidechain=True),
        entry("assistant", [{"type": "text", "text": "Watching."}], uuid="u2", sidechain=True),
    )


class TestWhoSaidIt:
    def test_a_prompt_the_person_typed_is_the_person_at_depth_zero(self, person_prompt: Transcript):
        found = attributes(only(records_of(person_prompt), "prompt"))
        assert found["agentic_memory.actor"] == "human"
        assert found["agentic_memory.actor.depth"] == "0"

    def test_a_prompt_an_orchestrator_wrote_is_an_agent_one_step_away(
        self, subagent_prompt: Transcript
    ):
        found = attributes(only(records_of(subagent_prompt), "prompt"))
        assert found["agentic_memory.actor"] == "agent"
        assert found["agentic_memory.actor.depth"] == "1"

    def test_a_subagents_reply_is_also_one_step_away(self, subagent_prompt: Transcript):
        found = attributes(only(records_of(subagent_prompt), "response"))
        assert found["agentic_memory.actor"] == "agent"
        assert found["agentic_memory.actor.depth"] == "1"


class TestTheScope:
    """The service never derives a scope. It records the one it was given."""

    def test_the_scope_the_client_derived_is_on_the_record(self, person_prompt: Transcript):
        found = attributes(only(records_of(person_prompt), "prompt"))
        assert (found["agentic_memory.scope"], found["agentic_memory.scope.kind"]) == (
            SCOPE,
            "repo",
        )

    def test_a_session_that_moves_keeps_the_scope_it_was_given(self):
        elsewhere = "/home/someone/src/example.test/acme/other"
        moved = json.loads(entry("user", "still the widget", uuid="u2"))
        moved["cwd"] = elsewhere
        found = [
            attributes(record)
            for record in records_of(
                transcript(entry("user", "hello", uuid="u1"), json.dumps(moved))
            )
        ]
        assert [one["agentic_memory.scope"] for one in found] == [SCOPE, SCOPE]
        assert found[1]["process.working_directory"] == elsewhere
