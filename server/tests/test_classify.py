"""Reading a window and scoring what kinds of memory are in it.

These cover the window, which is where a mistake is quiet: a window that is
too short loses the thing a reply replies to, and a window that keeps the
harness talking to itself scores plumbing as though the person had said it.
"""

import json
from types import SimpleNamespace

import pytest

from agentic_memory.classify import KINDS, KINDS_FINGERPRINT, classify, plumbing, spoken, window
from agentic_memory.settings import Settings


def said(occurred_at: int, kind: str, body: str) -> dict:
    """One record in a span, as the store hands it back."""
    return {"occurred_at": occurred_at, "kind": kind, "body": body}


def asked(occurred_at: int, scope_key: str | None = "github.com/liken-sh") -> dict:
    """The prompt a window is built around."""
    return {"occurred_at": occurred_at, "scope_key": scope_key}


class FakeStore:
    """The three queries a window is built from, and the write that follows."""

    def __init__(self, target, recent, span):
        self.target = target
        self.recent = recent
        self.span = span
        self.read: list[tuple] = []
        self.written: tuple | None = None

    async def fetchrow(self, query, *values):
        return self.target

    async def fetch(self, query, *values):
        self.read.append(values)
        return self.recent if "ORDER BY occurred_at DESC" in query else self.span

    async def execute(self, query, *values):
        self.written = (query, values)


class FakeModel:
    """A System One model that answers with what it was told to answer."""

    def __init__(self, **answers):
        self.answers = answers
        self.state: dict | None = None
        self.questions: dict | None = None

    async def system_one(self, *, state, questions):
        self.state, self.questions = state, questions
        return SimpleNamespace(
            nouls={kind: SimpleNamespace(noul=value) for kind, value in self.answers.items()}
        )


def one_round(body: str = "we use uv here") -> FakeStore:
    """A store holding a single prompt and nothing else."""
    return FakeStore(
        target=asked(100),
        recent=[{"occurred_at": 100}],
        span=[said(100, "prompt", body)],
    )


class TestPlumbing:
    def test_a_command_wrapper_is_not_the_person(self):
        assert plumbing("<command-name>/clear</command-name>")

    def test_an_injected_skill_is_not_the_person(self):
        assert plumbing("Base directory for this skill: /home/someone/.claude/skills/writing")

    def test_an_interrupt_marker_is_not_the_person(self):
        assert plumbing("[Request interrupted by user]")

    def test_what_the_person_typed_is_the_person(self):
        assert not plumbing("why is the dedup key on the repo revision?")


class TestSpoken:
    def test_both_sides_of_the_conversation_are_rendered(self):
        rendered = spoken([said(1, "prompt", "use uv"), said(2, "response", "noted")])
        assert rendered == "[person] use uv\n\n[agent] noted"

    def test_an_empty_agent_turn_is_left_out(self):
        # An agent turn arrives as many records and most of them hold no text.
        rendered = spoken([said(1, "prompt", "use uv"), said(2, "response", "")])
        assert rendered == "[person] use uv"

    def test_a_harness_entry_is_left_out(self):
        rendered = spoken([said(1, "prompt", "<command-name>/clear</command-name>")])
        assert rendered == ""


class TestWindow:
    async def test_the_window_ends_at_the_prompt_being_read(self):
        store = FakeStore(
            target=asked(100),
            recent=[{"occurred_at": 100}, {"occurred_at": 50}],
            span=[said(50, "prompt", "first"), said(100, "prompt", "second")],
        )
        found = await window(store, "s1", "e9", rounds=5)
        assert found.transcript == "[person] first\n\n[person] second"

    async def test_the_window_reaches_back_only_as_far_as_asked(self):
        store = FakeStore(target=asked(100), recent=[{"occurred_at": 100}], span=[])
        await window(store, "s1", "e9", rounds=2)
        assert store.read[0] == ("s1", 100, 2)

    async def test_the_window_starts_at_the_oldest_prompt_in_it(self):
        store = FakeStore(
            target=asked(100),
            recent=[{"occurred_at": 100}, {"occurred_at": 40}],
            span=[],
        )
        await window(store, "s1", "e9", rounds=2)
        assert store.read[1] == ("s1", 40, 100)

    async def test_a_prompt_that_is_not_in_the_record_is_nothing_to_read(self):
        store = FakeStore(target=None, recent=[], span=[])
        assert await window(store, "s1", "e9", rounds=5) is None

    async def test_the_window_names_the_scope_it_happened_in(self):
        store = FakeStore(
            target=asked(100, scope_key="github.com/liken-sh"),
            recent=[{"occurred_at": 100}],
            span=[said(100, "prompt", "hello")],
        )
        found = await window(store, "s1", "e9", rounds=5)
        assert found.scope_key == "github.com/liken-sh"


class TestClassify:
    async def test_the_verdicts_are_written_against_the_entry_that_was_read(self):
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=FakeModel(semantic=0.91, procedural=0.12),
        )
        _, values = store.written
        assert values[:2] == ("s1", "e9")
        assert values[2] == "github.com/liken-sh"

    async def test_the_probabilities_are_kept_rather_than_a_decision_about_them(self):
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=FakeModel(semantic=0.91, procedural=0.12),
        )
        _, values = store.written
        assert json.loads(values[7]) == {"semantic": 0.91, "procedural": 0.12}

    async def test_the_highest_probability_is_written_beside_them(self):
        # The next stage reads this column, so it is a range scan rather than a
        # walk through every verdict.
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=FakeModel(semantic=0.91, procedural=0.12),
        )
        _, values = store.written
        assert values[8] == 0.91

    async def test_the_reading_names_the_questions_it_was_asked(self):
        # The model name cannot do this job, because the questions move without
        # the model moving, and an answer to the old question is not an answer
        # to the new one.
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=FakeModel(semantic=0.91),
        )
        _, values = store.written
        assert values[4] == KINDS_FINGERPRINT

    async def test_the_model_is_asked_about_every_kind(self):
        store = one_round()
        model = FakeModel(semantic=0.91)
        await classify("s1", "e9", settings=Settings(), pool=store, client=model)
        assert model.questions == KINDS

    async def test_the_kinds_are_the_six_the_record_can_hold(self):
        assert set(KINDS) == {
            "semantic",
            "procedural",
            "prospective",
            "preference",
            "correction",
            "praise",
        }

    async def test_a_window_with_nothing_in_it_is_never_sent_to_a_model(self):
        store = FakeStore(target=None, recent=[], span=[])
        model = FakeModel(semantic=0.9)
        await classify("s1", "e9", settings=Settings(), pool=store, client=model)
        assert model.state is None

    async def test_nothing_is_written_when_the_model_answers_nothing(self):
        store = one_round()
        await classify("s1", "e9", settings=Settings(), pool=store, client=FakeModel())
        assert store.written is None


def test_the_fingerprint_changes_when_a_question_changes(monkeypatch):
    # The whole point of the fingerprint is that a reading under one question
    # set is not mistaken for a reading under another.
    from agentic_memory import classify as module

    before = module.questions_fingerprint()
    monkeypatch.setitem(
        module.KINDS,
        "semantic",
        module.KINDS["semantic"].model_copy(update={"instructions": "something else"}),
    )
    assert module.questions_fingerprint() != before


@pytest.mark.parametrize(
    "kind", ["semantic", "procedural", "prospective", "preference", "correction", "praise"]
)
def test_every_kind_asks_one_question(kind):
    question = KINDS[kind]
    assert question.instructions
    assert question.criteria["true"] and question.criteria["false"]
