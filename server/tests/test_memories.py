"""What a statement is worth now, and how one leaves the list.

The ranking is the part with numbers in it that nobody has measured, so these
tests pin the shape rather than the values: a kind that does not expire keeps a
floor under its weight, a kind that expires loses half its weight in a half-life
and falls out of the list, and a message can only end a statement that was put
in front of it.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from agentic_memory.memories import (
    CANDIDATES,
    UNRANKED,
    memories,
    ranked,
    retire,
    scored,
    standing,
    worth,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def statement(
    kind: str = "preference",
    age_in_days: float = 0.0,
    statement_id: int = 1,
    statement: str = "Postgres is the store.",
    written_in_days: float | None = None,
) -> dict:
    """One statement, as the store hands it back.

    The age is on `said_at`, which is when the message was said. `created_at` is
    when the statement was written, and a backfill makes that the same minute for
    a year of statements. `written_in_days` separates the two when a test needs
    them apart.
    """
    said = days_ago(age_in_days)
    written = said if written_in_days is None else days_ago(written_in_days)
    return {
        "id": statement_id,
        "statement": statement,
        "kind": kind,
        "score": 0.9,
        "scope_key": "github.com/liken-sh",
        "session_id": "s1",
        "entry_id": "e1",
        "model": "jev-1.13.0",
        "actor": "person",
        "actor_depth": 0,
        "said_at": said,
        "created_at": written,
    }


class FakePool:
    """A pool that answers a read and reports which updates took effect."""

    def __init__(self, rows: list[dict] | None = None, refuses: tuple = ()):
        self.rows = rows or []
        self.refuses = set(refuses)
        self.asked: list[tuple] = []
        self.ran: list[tuple] = []

    async def fetch(self, query, *values):
        self.asked.append(values)
        return self.rows

    async def execute(self, query, *values):
        self.ran.append(values)
        return "UPDATE 0" if values[0] in self.refuses else "UPDATE 1"


class TestWorth:
    def test_a_statement_written_just_now_is_worth_its_whole_weight(self):
        assert worth(statement("preference"), NOW) == pytest.approx(1.0)

    def test_a_kind_that_expires_loses_half_its_weight_in_a_half_life(self):
        assert worth(statement("prospective", 14), NOW) == pytest.approx(0.4 * 0.5)

    def test_a_kind_that_does_not_expire_keeps_its_floor(self):
        # Nothing in the record says the statement stopped being true, so age
        # cannot be allowed to take it away.
        assert worth(statement("preference", 3650), NOW) == pytest.approx(0.6, abs=0.01)

    def test_a_rule_from_a_year_ago_outranks_a_plan_from_today(self):
        assert worth(statement("preference", 365), NOW) > worth(statement("prospective"), NOW)

    def test_a_kind_nobody_weighed_ranks_below_every_kind_that_was(self):
        unknown = worth(statement("episodic"), NOW)
        assert unknown < worth(statement("prospective"), NOW)

    def test_an_unweighed_kind_is_worth_its_unweighed_weight(self):
        assert worth(statement("episodic"), NOW) == pytest.approx(UNRANKED[0])

    def test_a_statement_from_the_future_is_not_worth_more_than_a_new_one(self):
        assert worth(statement("preference", -30), NOW) == pytest.approx(1.0)

    def test_a_statement_with_no_known_moment_is_aged_from_when_it_was_written(self):
        row = statement("preference", 30)
        row["said_at"] = None
        assert worth(row, NOW) == pytest.approx(worth({**row, "said_at": row["created_at"]}, NOW))


class TestRanked:
    def test_the_best_statement_comes_first(self):
        rows = ranked(
            [statement("prospective", statement_id=1), statement("preference", statement_id=2)],
            NOW,
        )
        assert [row["id"] for row in rows] == [2, 1]

    def test_every_row_carries_the_rank_it_was_ordered_by(self):
        (row,) = ranked([statement("preference")], NOW)
        assert row["rank"] == pytest.approx(1.0)

    def test_two_statements_of_one_kind_are_ordered_by_the_newer_one(self):
        rows = ranked(
            [
                statement("preference", 30, statement_id=1),
                statement("preference", 0, statement_id=2),
            ],
            NOW,
        )
        assert [row["id"] for row in rows] == [2, 1]

    def test_the_statement_that_came_in_is_not_changed(self):
        one = statement("preference")
        ranked([one], NOW)
        assert "rank" not in one

    def test_scoring_leaves_the_rows_in_the_order_they_arrived(self):
        rows = [statement("prospective", statement_id=1), statement("preference", statement_id=2)]
        assert [row["id"] for row in scored(rows, NOW)] == [1, 2]


class TestStanding:
    async def test_the_scope_the_kinds_the_moment_and_the_cap_go_to_the_query(self):
        pool = FakePool()
        await standing(pool, scope_key="github.com/liken-sh", kinds=["correction"], said_before=NOW)
        assert pool.asked == [("github.com/liken-sh", ["correction"], NOW, CANDIDATES)]

    async def test_the_statements_come_back_as_plain_dicts(self):
        pool = FakePool(rows=[statement()])
        found = await standing(pool, scope_key=None, kinds=["correction"], said_before=NOW)
        assert found == [statement()]

    async def test_a_message_with_no_known_moment_is_offered_nothing(self):
        pool = FakePool(rows=[statement()])
        found = await standing(pool, scope_key=None, kinds=["correction"], said_before=None)
        assert found == []
        assert pool.asked == []

    async def test_hitting_the_cap_is_logged(self, caplog):
        pool = FakePool(rows=[statement(statement_id=n) for n in range(CANDIDATES)])
        with caplog.at_level(logging.INFO, logger="agentic_memory.memories"):
            await standing(pool, scope_key=None, kinds=["correction"], said_before=NOW)
        assert "older ones were not offered" in caplog.text

    async def test_staying_under_the_cap_is_not_logged(self, caplog):
        pool = FakePool(rows=[statement()])
        with caplog.at_level(logging.INFO, logger="agentic_memory.memories"):
            await standing(pool, scope_key=None, kinds=["correction"], said_before=NOW)
        assert caplog.text == ""


class TestMemories:
    async def test_the_read_is_narrowed_to_the_scope(self):
        pool = FakePool()
        await memories(pool, scope_key="github.com/liken-sh")
        assert pool.asked == [("github.com/liken-sh",)]

    async def test_the_limit_is_applied_after_the_ranking(self):
        pool = FakePool(
            rows=[
                statement("prospective", statement_id=1),
                statement("preference", statement_id=2),
            ]
        )
        found = await memories(pool, limit=1, now=NOW)
        assert [row["id"] for row in found] == [2]

    async def test_the_newest_order_is_by_when_the_writer_wrote_it(self):
        # A statement written just now can rank below one written yesterday,
        # which is the difference between the two orders.
        pool = FakePool(
            rows=[
                statement("prospective", statement_id=1, written_in_days=0),
                statement("preference", statement_id=2, written_in_days=1),
            ]
        )
        newest = await memories(pool, order="newest", now=NOW)
        by_rank = await memories(pool, order="rank", now=NOW)
        assert [row["id"] for row in newest] == [1, 2]
        assert [row["id"] for row in by_rank] == [2, 1]

    async def test_the_newest_order_is_narrowed_to_the_scope_too(self):
        pool = FakePool()
        await memories(pool, scope_key="github.com/liken-sh", order="newest")
        assert pool.asked == [("github.com/liken-sh",)]

    async def test_every_row_carries_a_rank_in_both_orders(self):
        # A reader that shows the two lists together needs to tell them apart.
        pool = FakePool(rows=[statement()])
        for order in ("rank", "newest"):
            found = await memories(pool, order=order, now=NOW)
            assert found[0]["rank"] == pytest.approx(1.0)


class TestRetire:
    async def test_a_statement_is_never_its_own_replacement(self):
        pool = FakePool()
        assert await retire(pool, replaced=[1], replacement=1) == []
        assert pool.ran == []

    async def test_the_statements_that_ended_come_back(self):
        pool = FakePool()
        assert await retire(pool, replaced=[7, 8], replacement=9) == [7, 8]

    async def test_a_statement_the_store_refused_does_not_come_back(self):
        # A statement that was already retired is not retired again, and the
        # caller hears about it rather than assuming it worked.
        pool = FakePool(refuses=[7])
        assert await retire(pool, replaced=[7, 8], replacement=9) == [8]

    async def test_the_moment_recorded_is_when_the_service_learned(self):
        pool = FakePool()
        await retire(pool, replaced=[7], replacement=9, now=NOW)
        assert pool.ran == [(7, 9, NOW)]
