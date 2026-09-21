"""Reading a Claude Code session file.

Every fixture here is invented. The shapes are the harness's; the words are not
anyone's.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from harnesses import claude_code

SESSION = "11111111-2222-3333-4444-555555555555"
CWD = "/home/someone/src/example.test/acme/widget"


def entry(kind: str, content: Any, *, uuid: str, sidechain: bool = False) -> dict[str, Any]:
    return {
        "type": kind,
        "uuid": uuid,
        "sessionId": SESSION,
        "cwd": CWD,
        "isSidechain": sidechain,
        "timestamp": "2026-09-21T12:00:00.000Z",
        "message": {"role": kind, "content": content},
    }


def session_file(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(found) + "\n" for found in entries))
    return path


def attributes(record: dict[str, Any]) -> dict[str, Any]:
    found = {}
    for pair in record["attributes"]:
        value = pair["value"]
        found[pair["key"]] = next(iter(value.values()))
    return found


def records_of(path: Path) -> list[dict[str, Any]]:
    return list(claude_code.read(path, machine="a-test-machine"))


def only(records: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [r for r in records if attributes(r)["agentic_memory.kind"] == kind]
    assert len(found) == 1
    return found[0]


@pytest.fixture
def result_as_text(tmp_path: Path) -> Path:
    return session_file(
        tmp_path,
        entry(
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "two files changed"}],
            uuid="u1",
        ),
    )


@pytest.fixture
def result_as_blocks(tmp_path: Path) -> Path:
    return session_file(
        tmp_path,
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
    def test_a_result_given_as_text_is_the_body(self, result_as_text: Path):
        found = only(records_of(result_as_text), "tool_result")
        assert found["body"]["stringValue"] == "two files changed"

    def test_a_result_given_as_blocks_is_joined_into_the_body(self, result_as_blocks: Path):
        found = only(records_of(result_as_blocks), "tool_result")
        assert found["body"]["stringValue"] == "first line\nsecond line"

    def test_the_result_is_the_tool_and_not_the_person(self, result_as_text: Path):
        found = only(records_of(result_as_text), "tool_result")
        assert attributes(found)["agentic_memory.actor"] == "agent"


@pytest.fixture
def person_prompt(tmp_path: Path) -> Path:
    return session_file(tmp_path, entry("user", "please fix the widget", uuid="u1"))


@pytest.fixture
def subagent_prompt(tmp_path: Path) -> Path:
    return session_file(
        tmp_path,
        entry("user", "You are a watch agent for the widget.", uuid="u1", sidechain=True),
        entry("assistant", [{"type": "text", "text": "Watching."}], uuid="u2", sidechain=True),
    )


class TestWhoSaidIt:
    def test_a_prompt_the_person_typed_is_the_person_at_depth_zero(self, person_prompt: Path):
        found = attributes(only(records_of(person_prompt), "prompt"))
        assert found["agentic_memory.actor"] == "human"
        assert found["agentic_memory.actor.depth"] == "0"

    def test_a_prompt_an_orchestrator_wrote_is_an_agent_one_step_away(self, subagent_prompt: Path):
        found = attributes(only(records_of(subagent_prompt), "prompt"))
        assert found["agentic_memory.actor"] == "agent"
        assert found["agentic_memory.actor.depth"] == "1"

    def test_a_subagents_reply_is_also_one_step_away(self, subagent_prompt: Path):
        found = attributes(only(records_of(subagent_prompt), "response"))
        assert found["agentic_memory.actor"] == "agent"
        assert found["agentic_memory.actor.depth"] == "1"
