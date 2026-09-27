"""Rescheduling the work a failed model call left undone.

A model call that runs out of retries leaves no trace in the tables classify
and synthesize write, only a row in the ledger that says it happened. These
cover what the sweep finds from that row and what it does with it: a failed
call's entry comes back, a refused one never does, an entry that already has
its reading or its statement is not offered twice, and an entry that keeps
failing past the attempt cap is left alone.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentic_memory.classify import (
    KIND_COLUMNS,
    classify,
    questions_fingerprint,
    statement_key,
    task_key,
)
from agentic_memory.settings import Settings
from agentic_memory.sweep import sweep_failures, unread_prompts, unwritten_readings

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=6)

# The cap tests pin, apart from the setting's own default. A test that read
# the default instead would break the moment the default did, for a reason
# that has nothing to do with what it is testing.
CAP = 12


async def logged(
    store,
    *,
    session_id: str = "s1",
    entry_id: str = "e1",
    task: str = "classify",
    outcome: str = "error",
    called_at: datetime = NOW,
) -> None:
    """One ledger row, as a call would leave it."""
    await store.execute(
        """
        INSERT INTO model_calls (provider, task, session_id, entry_id, outcome, called_at)
        VALUES ('typesafe', $1, $2, $3, $4, $5)
        """,
        task,
        session_id,
        entry_id,
        outcome,
        called_at,
    )


async def failed(
    store, *, session_id: str = "s1", entry_id: str = "e1", task: str, times: int
) -> None:
    """Several error rows for one entry, as repeated sweeps would leave it."""
    for _ in range(times):
        await logged(store, session_id=session_id, entry_id=entry_id, task=task, outcome="error")


async def classified(
    store, *, session_id: str = "s1", entry_id: str = "e1", fingerprint: str | None = None, **scores
) -> None:
    """One reading, as classify() would have written it."""
    columns = dict.fromkeys(KIND_COLUMNS, 0.05) | scores
    await store.execute(
        f"""
        INSERT INTO classifications
            (session_id, entry_id, model, questions_fingerprint, rounds, state,
             {", ".join(columns)})
        VALUES ($1, $2, 'jev-1.13.0', $3, 5, '{{}}',
                {", ".join(f"${n}" for n in range(4, 4 + len(columns)))})
        """,
        session_id,
        entry_id,
        fingerprint or questions_fingerprint(),
        *columns.values(),
    )


async def written(
    store, *, session_id: str = "s1", entry_id: str = "e1", fingerprint: str | None = None
) -> None:
    """One statement, as synthesize() would have written it."""
    await store.execute(
        """
        INSERT INTO memories
            (statement, kind, score, session_id, entry_id, model, questions_fingerprint)
        VALUES ('a statement', 'preference', 0.9, $1, $2, 'jev-1.13.0', $3)
        """,
        session_id,
        entry_id,
        fingerprint or questions_fingerprint(),
    )


class TestUnreadPrompts:
    async def test_an_entry_whose_only_call_errored_is_found(self, store):
        await logged(store, outcome="error")
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == [("s1", "e1")]
        assert found.abandoned == 0

    async def test_a_refused_entry_is_never_found(self, store):
        # The provider read the request and answered. Sending it again gets
        # the same answer, so a refusal is not a reason to sweep.
        await logged(store, outcome="refused")
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_an_entry_with_a_reading_already_is_not_found(self, store):
        await logged(store, outcome="error")
        await classified(store)
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_an_error_before_the_lookback_is_not_found(self, store):
        await logged(store, outcome="error", called_at=SINCE - timedelta(hours=1))
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_an_error_followed_by_a_later_success_is_not_found(self, store):
        # The latest call is what answers whether the entry is still failing,
        # and this one is not.
        await logged(store, outcome="error", called_at=NOW - timedelta(minutes=5))
        await logged(store, outcome="ok", called_at=NOW)
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_a_reading_under_a_stale_question_set_does_not_count(self, store):
        # The old reading answered the old questions, not the ones asked now.
        await logged(store, outcome="error")
        await classified(store, fingerprint="stale-fingerprint")
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == [("s1", "e1")]

    async def test_two_failed_entries_both_come_back(self, store):
        await logged(store, entry_id="e1", outcome="error")
        await logged(store, entry_id="e2", outcome="error")
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert set(found.ready) == {("s1", "e1"), ("s1", "e2")}

    async def test_a_synthesize_error_is_not_a_classify_failure(self, store):
        await logged(store, task="synthesize", outcome="error")
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_a_four_day_old_error_is_found_with_the_default_lookback(self, store):
        # The outage that motivated the sweep lasted an afternoon, and nobody
        # noticed for days. The default lookback has to outlive that.
        await logged(store, outcome="error", called_at=NOW - timedelta(days=4))
        settings = Settings()
        found = await unread_prompts(
            store, since=NOW - settings.sweep_lookback, sweep_attempts=settings.sweep_attempts
        )
        assert found.ready == [("s1", "e1")]

    async def test_an_entry_under_the_cap_is_still_offered(self, store):
        await failed(store, task="classify", times=CAP - 1)
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == [("s1", "e1")]
        assert found.abandoned == 0

    async def test_an_entry_at_the_cap_is_abandoned_instead(self, store):
        # "Reach the cap" means at it, not only past it, or the entry would
        # get one extra try the setting did not promise it.
        await failed(store, task="classify", times=CAP)
        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []
        assert found.abandoned == 1


class TestUnwrittenReadings:
    async def test_a_reading_that_cleared_a_threshold_and_failed_to_write_is_found(self, store):
        await logged(store, task="synthesize", outcome="error")
        await classified(store, semantic=0.95)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == [("s1", "e1")]

    async def test_a_reading_that_cleared_no_threshold_is_not_found(self, store):
        await logged(store, task="synthesize", outcome="error")
        await classified(store)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_a_reading_with_a_statement_already_is_not_found(self, store):
        await logged(store, task="synthesize", outcome="error")
        await classified(store, semantic=0.95)
        await written(store)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_a_refused_synthesize_call_is_never_found(self, store):
        await logged(store, task="synthesize", outcome="refused")
        await classified(store, semantic=0.95)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    async def test_a_classify_error_is_not_a_synthesize_failure(self, store):
        await logged(store, task="classify", outcome="error")
        await classified(store, semantic=0.95)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == []

    @pytest.mark.parametrize(("times", "abandoned"), [(CAP - 1, 0), (CAP, 1), (CAP + 1, 1)])
    async def test_the_cap_is_read_from_error_attempts_too(self, store, times, abandoned):
        await failed(store, task="synthesize", times=times)
        await classified(store, semantic=0.95)
        found = await unwritten_readings(store, since=SINCE, sweep_attempts=CAP)
        assert found.abandoned == abandoned
        assert found.ready == ([] if abandoned else [("s1", "e1")])


class FakeDocket:
    """A docket that records what was scheduled instead of scheduling it."""

    def __init__(self):
        self.scheduled: list[tuple[str | None, tuple]] = []

    def add(self, task, *, key=None):
        async def scheduled(*args):
            self.scheduled.append((key, args))

        return scheduled


class TestSweepFailures:
    async def test_a_failed_classify_entry_is_rescheduled_under_the_live_key(self, store):
        await logged(store, outcome="error")
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        assert docket.scheduled == [(task_key("s1", "e1"), ("s1", "e1", "sweep"))]

    async def test_a_failed_synthesize_entry_is_rescheduled_under_the_live_key(self, store):
        await logged(store, task="synthesize", outcome="error")
        await classified(store, semantic=0.95)
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        assert docket.scheduled == [(statement_key("s1", "e1"), ("s1", "e1", "sweep"))]

    async def test_an_entry_that_already_has_a_reading_is_not_rescheduled(self, store):
        await logged(store, outcome="error")
        await classified(store)
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        assert docket.scheduled == []

    async def test_a_refused_entry_is_never_rescheduled(self, store):
        await logged(store, outcome="refused")
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        assert docket.scheduled == []

    async def test_the_run_scheduled_work_carries_is_sweep(self, store):
        await logged(store, outcome="error")
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        (_, args) = docket.scheduled[0]
        assert args[-1] == "sweep"

    async def test_an_entry_past_the_cap_is_not_rescheduled(self, store):
        await failed(store, task="classify", times=Settings().sweep_attempts + 1)
        docket = FakeDocket()
        await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        assert docket.scheduled == []

    async def test_one_warning_names_the_count_given_up_on(self, store, caplog):
        await failed(store, entry_id="e1", task="classify", times=Settings().sweep_attempts + 1)
        await failed(store, entry_id="e2", task="classify", times=Settings().sweep_attempts + 1)
        docket = FakeDocket()
        with caplog.at_level(logging.WARNING, logger="agentic_memory.sweep"):
            await sweep_failures(settings=Settings(), pool=store, docket=docket, now=NOW)
        warnings = [record for record in caplog.records if "attempt cap" in record.message]
        assert len(warnings) == 1
        assert "2 entries" in warnings[0].message


class AnsweringModel:
    """A System One model that answers every kind of memory the same way."""

    def __init__(self, **scores):
        self.scores = {**dict.fromkeys(KIND_COLUMNS, 0.05), **scores}
        self.model = "jev-1.13.0"

    async def system_one(self, *, state, questions):
        return SimpleNamespace(
            model=self.model,
            nouls={kind: SimpleNamespace(noul=value) for kind, value in self.scores.items()},
        )


class TestSweptWorkIsRead:
    """Proof that scheduling under `sweep` is scheduling the same work the
    live path does: running what the sweep hands over clears the failure it
    found.
    """

    async def test_the_entry_the_sweep_finds_is_read_once_classify_runs(self, store):
        await store.execute(
            "INSERT INTO resources (fingerprint, resource) VALUES ('r', '{}') "
            "ON CONFLICT DO NOTHING"
        )
        await store.execute("INSERT INTO scopes (fingerprint) VALUES ('s') ON CONFLICT DO NOTHING")
        export_id = await store.fetchval(
            """
            INSERT INTO otel_exports (session_id, entry_id, resource_id, scope_id, record)
            VALUES ('s1', 'e1', (SELECT id FROM resources), (SELECT id FROM scopes), '{}')
            RETURNING id
            """
        )
        await store.execute(
            """
            INSERT INTO logs
                (export_id, received_at, resource_id, scope_id, occurred_at, attributes,
                 session_id, entry_id, kind, body)
            VALUES ($1, now(), (SELECT id FROM resources), (SELECT id FROM scopes), now(), '{}',
                    's1', 'e1', 'prompt', 'we use uv, not pip')
            """,
            export_id,
        )
        await logged(store, outcome="error")

        found = await unread_prompts(store, since=SINCE, sweep_attempts=CAP)
        assert found.ready == [("s1", "e1")]

        (session_id, entry_id) = found.ready[0]
        await classify(
            session_id,
            entry_id,
            "sweep",
            settings=Settings(),
            pool=store,
            client=AnsweringModel(semantic=0.91),
            docket=FakeDocket(),
        )
        assert (await unread_prompts(store, since=SINCE, sweep_attempts=CAP)).ready == []
