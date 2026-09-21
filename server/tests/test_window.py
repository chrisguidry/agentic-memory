"""Cutting a window down to what the model will take.

The provider refuses a request over its token budget, and a coding session
produces messages that pass it: a pasted file, or a page of tool output. The
cut has to keep the message, which is what is judged, and give up the
exchanges before it first.
"""

from agentic_memory.window import Window, plumbing, spoken


class TestPlumbing:
    def test_a_command_wrapper_is_not_the_person(self):
        assert plumbing("<command-name>/clear</command-name>")

    def test_an_injected_skill_is_not_the_person(self):
        assert plumbing("Base directory for this skill: /home/someone/.claude/skills/writing")

    def test_an_interrupt_marker_is_not_the_person(self):
        assert plumbing("[Request interrupted by user]")

    def test_a_compaction_summary_is_not_the_person(self):
        assert plumbing(
            "This session is being continued from a previous conversation that ran out "
            "of context. The summary is below."
        )

    def test_feedback_from_a_stop_hook_is_not_the_person(self):
        assert plumbing("Stop hook feedback:\n[a hook said something]")

    def test_what_the_person_typed_is_the_person(self):
        assert not plumbing("why is the dedup key on the repo revision?")


def said(occurred_at: int, kind: str, body: str) -> dict:
    """One record in a span, as the store hands it back."""
    return {"occurred_at": occurred_at, "kind": kind, "body": body}


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


def a_window(message: str, before: str = "") -> Window:
    return Window(message=message, before=before, scope_key=None, actor="human", actor_depth=0)


class TestFitted:
    def test_a_window_inside_the_budget_is_left_alone(self):
        found = a_window("short", "[person] hi\n\n[agent] hello")
        assert found.fitted(1000) is found

    def test_the_exchanges_before_the_message_give_way_first(self):
        before = "[person] first\n\n[agent] one\n\n[person] second\n\n[agent] two"
        found = a_window("the message", before).fitted(len("the message") + 30)
        assert found.message == "the message"
        assert found.before == "[person] second\n\n[agent] two"

    def test_the_cut_falls_at_a_turn_boundary(self):
        before = "[person] first\n\n[agent] one\n\n[person] second\n\n[agent] two"
        found = a_window("m", before).fitted(len("m") + 20)
        assert found.before.startswith("[")

    def test_nothing_before_survives_when_no_whole_turn_fits(self):
        found = a_window("m", "[person] a long first turn").fitted(len("m") + 5)
        assert found.before == ""

    def test_a_message_over_the_budget_keeps_its_opening_and_its_ending(self):
        message = "A" * 500 + "B" * 500 + "C" * 500
        found = a_window(message, "[person] earlier").fitted(1000)
        assert found.message.startswith("A" * 500)
        assert found.message.endswith("C" * 500)
        assert "[... 500 characters left out ...]" in found.message
        assert found.before == ""

    def test_the_cut_message_says_how_much_was_left_out(self):
        found = a_window("x" * 1300).fitted(1000)
        assert "[... 300 characters left out ...]" in found.message
