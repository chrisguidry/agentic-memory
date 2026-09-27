"""A message that replaces statements the place already holds.

Only a message that pushes back on something is shown the held statements, and
it can end only a statement that was put in front of it.
"""

from _writer import SAID, FakeStore, held, reading, replying

from agentic_memory.settings import Settings
from agentic_memory.synthesize import write


class TestReplacing:
    async def test_a_message_that_does_not_push_back_is_offered_nothing(self):
        store = FakeStore(reading(semantic=0.95), standing=[held(7)])
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.asked_standing is None
        assert store.retired == []

    async def test_a_message_that_corrects_something_older_opens_the_gate(self):
        # The gate is separate from the kinds, so a message can replace
        # something without the correction kind firing.
        store = FakeStore(reading(semantic=0.95, corrects_earlier=0.9), standing=[held(7)])
        model = replying(("semantic", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.asked_standing is not None

    async def test_the_held_statements_are_asked_for_at_the_place_and_kind(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        scope_key, said_before, kinds, _ = store.asked_standing
        assert scope_key == "github.com/liken-sh"
        assert kinds == ["correction"]
        assert said_before == SAID

    async def test_the_held_statements_are_listed_for_the_writer(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "[1] (preference) Postgres is the store." in model.asked

    async def test_a_message_with_nothing_held_asks_without_a_list(self):
        store = FakeStore(reading(correction=0.95))
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert "already holds" not in model.asked

    async def test_a_replacement_ends_the_statement_it_names(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "The store is Redis."), replaces=[1])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (retired,) = store.retired
        assert retired[0] == 7
        assert retired[1] == 1

    async def test_a_number_that_was_never_offered_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=[99])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_number_written_as_text_is_read(self):
        # The model answers with numbers and with the numbers as text, and both
        # mean the same thing.
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=["1"])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        (retired,) = store.retired
        assert retired[0] == 7

    async def test_an_answer_that_is_not_a_number_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces=["the first one"])
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_replacement_that_was_not_a_list_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"), replaces="1")
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []

    async def test_a_statement_that_replaces_nothing_ends_nothing(self):
        store = FakeStore(reading(correction=0.95), standing=[held(7)])
        model = replying(("correction", "x"))
        await write("s1", "e1", settings=Settings(), pool=store, client=model)
        assert store.retired == []
