"""Reading a window and scoring what kinds of memory are in it.

These cover the window, which is where a mistake is quiet. A window that is
too short loses the thing a reply replies to, and a question that judges the
window rather than the message scores whatever was corrected earlier.
"""

import inspect
from types import SimpleNamespace

import pytest

from agentic_memory.classify import (
    KIND_COLUMNS,
    KINDS,
    KINDS_FINGERPRINT,
    classify,
    readable_prompts,
    task_key,
    worth_reading,
)
from agentic_memory.settings import Settings
from agentic_memory.window import window


def said(occurred_at: int, kind: str, body: str) -> dict:
    """One record in a span, as the store hands it back."""
    return {"occurred_at": occurred_at, "kind": kind, "body": body}


def asked(
    occurred_at: int,
    body: str = "we use uv here",
    scope_key="github.com/liken-sh",
    actor="person",
    actor_depth: int = 0,
) -> dict:
    """The message a window is built around."""
    return {
        "occurred_at": occurred_at,
        "scope_key": scope_key,
        "body": body,
        "actor": actor,
        "actor_depth": actor_depth,
    }


class FakeStore:
    """The three queries a window is built from, and the write that follows."""

    def __init__(self, target, recent, before):
        self.target = target
        self.recent = recent
        self.before = before
        self.read: list[tuple] = []
        self.written: tuple | None = None

    async def fetchrow(self, query, *values):
        return self.target

    async def fetch(self, query, *values):
        self.read.append(values)
        return self.recent if "ORDER BY occurred_at DESC" in query else self.before

    async def execute(self, query, *values):
        self.written = (query, values)


class FakeDocket:
    """A docket that records what was scheduled instead of scheduling it."""

    def __init__(self):
        self.scheduled: list[tuple[str | None, tuple]] = []

    def add(self, task, *, key=None):
        # The real one is sync and returns something awaitable, so a fake that
        # is async would pass a coroutine where a callable is expected.
        async def scheduled(*args):
            self.scheduled.append((key, args))

        return scheduled


class FakeModel:
    """A System One model that answers with what it was told to answer."""

    def __init__(self, **answers):
        self.answers = answers
        self.state: dict | None = None
        self.questions: dict | None = None
        # The versioned id a real response carries, not the alias it was asked
        # for, because the alias moves and the answers move with it.
        self.model = "jev-1.13.0"

    async def system_one(self, *, state, questions):
        self.state, self.questions = state, questions
        return SimpleNamespace(
            model=self.model,
            nouls={kind: SimpleNamespace(noul=value) for kind, value in self.answers.items()},
        )


def one_round(message: str = "we use uv here") -> FakeStore:
    """A store holding one message and nothing before it."""
    return FakeStore(
        target=asked(100, message),
        recent=[{"occurred_at": 100}],
        before=[],
    )


def answering(**overrides) -> FakeModel:
    """A model that answers every kind, with the named ones overridden.

    The task refuses a partial answer, so a test that wants a reading has to
    supply one of these rather than a couple of kinds.
    """
    return FakeModel(**{**dict.fromkeys(KINDS, 0.05), **overrides})


class TestWorthReading:
    def test_a_prompt_a_person_typed_is_read(self):
        written = [("s1", "e1", "why is the dedup key on the repo revision?")]
        assert worth_reading(written) == [("s1", "e1")]

    def test_an_entry_the_harness_wrote_is_not_scheduled(self):
        # A third of stored prompts are the harness talking to itself, and a
        # scheduled task for one of them is a task with nothing to do.
        written = [("s1", "e1", "<command-name>/clear</command-name>")]
        assert worth_reading(written) == []

    def test_the_body_does_not_come_back_with_the_pair(self):
        assert worth_reading([("s1", "e1", "hello")]) == [("s1", "e1")]

    def test_a_real_prompt_beside_a_harness_entry_is_still_read(self):
        written = [
            ("s1", "e1", "<command-name>/clear</command-name>"),
            ("s1", "e2", "why is the dedup key on the repo revision?"),
        ]
        assert worth_reading(written) == [("s1", "e2")]


class TestTaskKey:
    def test_the_question_set_is_part_of_the_name(self):
        # Reading the same message again under new questions is its own piece of
        # work, not a message that was already handled.
        assert KINDS_FINGERPRINT in task_key("s1", "e1")

    def test_two_messages_get_two_names(self):
        assert task_key("s1", "e1") != task_key("s1", "e2")

    def test_two_sessions_get_two_names(self):
        assert task_key("s1", "e1") != task_key("s2", "e1")


class ReadingStore:
    """A store that answers the range query with rows it was handed."""

    def __init__(self, rows):
        self.rows = rows
        self.wanted: tuple | None = None

    async def fetch(self, query, *values):
        self.wanted = values
        return self.rows


class TestReadablePrompts:
    async def test_only_the_persons_own_words_come_back(self):
        store = ReadingStore(
            [
                {"session_id": "s1", "entry_id": "e1", "body": "why is the key on it?"},
                {
                    "session_id": "s1",
                    "entry_id": "e2",
                    "body": "<command-name>/clear</command-name>",
                },
            ]
        )
        found = await readable_prompts(store, since=1, until=2, limit=10)
        assert found == [("s1", "e1")]

    async def test_the_pair_comes_back_without_the_body(self):
        store = ReadingStore([{"session_id": "s1", "entry_id": "e1", "body": "hello"}])
        found = await readable_prompts(store, since=1, until=2, limit=10)
        assert found == [("s1", "e1")]


class TestWindow:
    async def test_the_message_is_the_prompt_being_read(self):
        store = FakeStore(
            target=asked(100, "the message"), recent=[{"occurred_at": 100}], before=[]
        )
        found = await window(store, "s1", "e9", rounds=5)
        assert found.message == "the message"

    async def test_the_exchanges_before_it_are_kept_separate(self):
        # The questions judge the message and only use the rest to read it, so
        # the two cannot be handed over as one lump.
        store = FakeStore(
            target=asked(100, "the message"),
            recent=[{"occurred_at": 100}, {"occurred_at": 50}],
            before=[said(50, "prompt", "earlier"), said(80, "response", "and its answer")],
        )
        found = await window(store, "s1", "e9", rounds=5)
        assert found.before == "[person] earlier\n\n[agent] and its answer"

    async def test_the_message_is_not_repeated_in_what_came_before(self):
        store = FakeStore(
            target=asked(100, "the message"),
            recent=[{"occurred_at": 100}],
            before=[],
        )
        await window(store, "s1", "e9", rounds=5)
        assert store.read[1] == ("s1", 100, 100)

    async def test_the_window_reaches_back_only_as_far_as_asked(self):
        store = FakeStore(target=asked(100), recent=[{"occurred_at": 100}], before=[])
        await window(store, "s1", "e9", rounds=2)
        assert store.read[0] == ("s1", 100, 2)

    async def test_the_window_starts_at_the_oldest_prompt_in_it(self):
        store = FakeStore(
            target=asked(100),
            recent=[{"occurred_at": 100}, {"occurred_at": 40}],
            before=[],
        )
        await window(store, "s1", "e9", rounds=2)
        assert store.read[1] == ("s1", 40, 100)

    async def test_a_prompt_that_is_not_in_the_record_is_nothing_to_read(self):
        store = FakeStore(target=None, recent=[], before=[])
        assert await window(store, "s1", "e9", rounds=5) is None

    async def test_the_window_names_the_scope_it_happened_in(self):
        store = FakeStore(target=asked(100), recent=[{"occurred_at": 100}], before=[])
        found = await window(store, "s1", "e9", rounds=5)
        assert found.scope_key == "github.com/liken-sh"


class RefusingModel(FakeModel):
    """A System One model that refuses the request, as the provider does over budget."""

    async def system_one(self, *, state, questions):
        from typesafe_sdk import TypeSafeBadRequestError

        raise TypeSafeBadRequestError(400, {"detail": {"error_type": "max_tokens_exceeded"}}, {})


class TestClassify:
    async def test_a_request_the_model_refuses_is_a_defect_and_not_retried(self):
        # A refusal comes back the same on every attempt, so raising it would
        # spend the retry policy on a request that cannot succeed.
        store = one_round()
        await classify(
            "s1", "e9", settings=Settings(), pool=store, client=RefusingModel(), docket=FakeDocket()
        )
        assert store.written is None

    async def test_the_window_is_cut_to_the_budget_before_it_is_sent(self):
        store = FakeStore(
            target=asked(100, "the message"),
            recent=[{"occurred_at": 100}, {"occurred_at": 50}],
            before=[said(50, "prompt", "a" * 500), said(80, "response", "b" * 50)],
        )
        model = answering()
        await classify(
            "s1",
            "e9",
            settings=Settings(classify_budget=100),
            pool=store,
            client=model,
            docket=FakeDocket(),
        )
        assert model.state["message"] == "the message"
        assert model.state["before"] == "[agent] " + "b" * 50

    async def test_the_state_holds_the_message_apart_from_what_came_before(self):
        store = FakeStore(
            target=asked(100, "the message"),
            recent=[{"occurred_at": 100}, {"occurred_at": 50}],
            before=[said(50, "prompt", "earlier")],
        )
        model = FakeModel(semantic=0.9)
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=model,
            docket=FakeDocket(),
        )
        assert model.state == {"before": "[person] earlier", "message": "the message"}

    async def test_the_verdicts_are_written_against_the_entry_that_was_read(self):
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=answering(semantic=0.91, procedural=0.12),
            docket=FakeDocket(),
        )
        _, values = store.written
        assert values[:2] == ("s1", "e9")
        assert values[2] == "github.com/liken-sh"

    async def test_the_scores_land_in_the_column_for_their_kind(self):
        # The columns and the questions come from one tuple, so a kind cannot be
        # asked about and then written under another kind's name.
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=answering(**{kind: 0.5 for kind in KINDS}),
            docket=FakeDocket(),
        )
        _, values = store.written
        written = dict(zip(KIND_COLUMNS, values[9:], strict=True))
        assert written == dict.fromkeys(KINDS, 0.5)

    async def test_the_probabilities_are_kept_rather_than_a_decision_about_them(self):
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=answering(semantic=0.91, procedural=0.12),
            docket=FakeDocket(),
        )
        _, values = store.written
        written = dict(zip(KIND_COLUMNS, values[9:], strict=True))
        assert written["semantic"] == 0.91
        assert written["procedural"] == 0.12

    async def test_the_versioned_model_is_written_rather_than_the_alias(self):
        # Pin the version when tuning thresholds: the alias moves and the
        # answers move with it, and the column would not show that it happened.
        store = one_round()
        model = answering(semantic=0.91)
        model.model = "jev-1.13.0"
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=model,
            docket=FakeDocket(),
        )
        _, values = store.written
        assert values[3] == "jev-1.13.0"

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
            client=answering(semantic=0.91),
            docket=FakeDocket(),
        )
        _, values = store.written
        assert values[4] == KINDS_FINGERPRINT

    async def test_the_model_is_asked_about_every_kind(self):
        store = one_round()
        model = FakeModel(semantic=0.91)
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=model,
            docket=FakeDocket(),
        )
        assert model.questions == KINDS

    async def test_the_kinds_are_the_eleven_the_record_can_hold(self):
        # Named rather than counted, so adding a question means changing this
        # test on purpose and adding the column it writes.
        assert set(KINDS) == {
            "semantic",
            "procedural",
            "prospective",
            "preference",
            "correction",
            "praise",
            "corrects_earlier",
            "praise_outcome",
            "about_artifact",
            "beyond_this_project",
            "forbids",
        }

    async def test_a_window_with_nothing_in_it_is_never_sent_to_a_model(self):
        store = FakeStore(target=None, recent=[], before=[])
        model = FakeModel(semantic=0.9)
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=model,
            docket=FakeDocket(),
        )
        assert model.state is None

    async def test_a_partial_answer_is_not_written(self):
        # A reading missing a kind cannot be compared with one that has it, and
        # the task is idempotent, so a later pass can read the message again.
        store = one_round()
        await classify(
            "s1",
            "e9",
            settings=Settings(),
            pool=store,
            client=FakeModel(semantic=0.91),
            docket=FakeDocket(),
        )
        assert store.written is None


@pytest.mark.parametrize("kind", KINDS)
def test_every_question_judges_the_message_rather_than_the_window(kind):
    # Scoring the window means every message inherits whatever was corrected
    # earlier in it, which is how a message about a docket task scored 0.91 on
    # correction.
    question = KINDS[kind]
    assert question.instructions["inspect"] == "`message`"
    assert "`message`" in question.instructions["question"]


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_asks_one_question(kind):
    question = KINDS[kind]
    assert question.instructions["question"]
    assert question.criteria["true"] and question.criteria["false"]


def test_the_fingerprint_changes_when_a_question_changes(monkeypatch):
    # The whole point of the fingerprint is that a reading under one question
    # set is not mistaken for a reading under another.
    from agentic_memory import classify as module

    before = module.questions_fingerprint()
    monkeypatch.setitem(
        module.KINDS,
        "semantic",
        module.KINDS["semantic"].model_copy(update={"instructions": {"question": "else"}}),
    )
    assert module.questions_fingerprint() != before


class TestRetry:
    def test_a_reading_is_tried_again_when_the_provider_fails(self):
        retry = inspect.signature(classify).parameters["retry"].default
        assert retry.attempts > 1

    def test_the_tries_stop(self):
        retry = inspect.signature(classify).parameters["retry"].default
        assert retry.attempts < 10
