"""A probe recall: what a turn would be handed, with nothing recorded.

These post to the routes, because the promise is about the store: a probe is
handed what a live ask is handed and leaves no injection row behind.
"""

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

import agentic_memory.app as app_module
import agentic_memory.probe as probe_module
from agentic_memory.app import app
from agentic_memory.embed import Embedder
from agentic_memory.metrics import REGISTRY
from agentic_memory.settings import Settings

SESSION = "11111111-2222-3333-4444-555555555555"
ASK = {
    "session_id": SESSION,
    "harness": "claude-code",
    "scope_key": "example.test/acme/widget",
    "prompt": "how do the tests talk to the database?",
}


@pytest.fixture
async def service(
    store: asyncpg.Pool, embedder: Embedder, monkeypatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The service in the client's own process, with two statements to hand out."""
    await store.execute(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, actor, actor_depth)
        VALUES
            ('Tests come before code here.', 'preference', 0.9,
             'example.test/acme/widget', 's1', 'e1', 'jev-1.13.0', 'fp', now(), 'human', 0),
            ('The suite runs against a real Postgres.', 'fact', 0.9,
             'example.test/acme/widget', 's1', 'e2', 'jev-1.13.0', 'fp', now(), 'human', 0)
        """
    )
    app.state.pool = store
    app.state.embedder = embedder
    # The routes read settings fresh on every call; turn the opening list on so
    # a session's first ask here has the two statements above to hand out.
    opened = Settings(recall_opening_limit=3)
    monkeypatch.setattr(app_module, "get_settings", lambda: opened)
    monkeypatch.setattr(probe_module, "get_settings", lambda: opened)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
        yield client


async def handed(client: httpx.AsyncClient, ask: dict, route: str = "/recall") -> list[int]:
    answer = await client.post(route, json=ask)
    assert answer.status_code == 200
    return [found["id"] for found in answer.json()["statements"]]


async def probed(client: httpx.AsyncClient, seen: list[int] | None = None) -> list[int]:
    return await handed(client, {**ASK, "seen": seen or []}, "/recall/probe")


async def rows(store: asyncpg.Pool) -> int:
    return await store.fetchval("SELECT count(*) FROM injections")


async def test_a_probe_is_handed_what_a_live_ask_is_handed(service, store):
    probe = await probed(service)
    assert probe
    assert await handed(service, ASK) == probe


async def test_a_probe_writes_no_injection_row(service, store):
    await probed(service)
    assert await rows(store) == 0


async def test_every_probe_in_a_session_takes_the_opening_form(service, store):
    first = await probed(service)
    assert await probed(service) == first


async def test_what_a_probe_session_was_handed_is_not_handed_again(service, store):
    first = await probed(service)
    assert await probed(service, first) == []


# A client sends these fields to the live route only by mistake, and a live
# route that ignored them would record a probe as a live turn.
@pytest.mark.parametrize("field", [{"probe": True}, {"seen": [1]}, {"probe": False}])
async def test_the_live_route_refuses_the_fields_of_a_probe(service, store, field):
    answer = await service.post("/recall", json={**ASK, **field})
    assert answer.status_code == 422
    assert await rows(store) == 0


# A probe carries no count of missed deadlines, because the live count holds
# only what a person's sessions missed.
async def test_the_probe_route_refuses_a_count_of_misses(service):
    answer = await service.post("/recall/probe", json={**ASK, "missed": 1})
    assert answer.status_code == 422


def counted(name: str, form: str, outcome: str) -> float:
    found = REGISTRY.get_sample_value(f"{name}_total", {"form": form, "outcome": outcome})
    return found or 0.0


@pytest.mark.parametrize(
    "name, change",
    [("agentic_memory_probe_recalls", 1.0), ("agentic_memory_recalls", 0.0)],
)
async def test_a_probe_is_counted_apart_from_live_recalls(service, name, change):
    before = counted(name, "opening", "handed")
    await probed(service)
    assert counted(name, "opening", "handed") - before == change
