"""The recall route, served by a real server to a real client.

The bastion gives up on a recall after its deadline and drops the connection.
Whether the service still writes the injection row then depends on the server
reporting the dropped connection to the route, so these run uvicorn on a port
and not the app in the client's own process.
"""

import asyncio
from collections.abc import AsyncIterator

import _postgres
import asyncpg
import httpx
import pytest
import uvicorn

from agentic_memory.app import app
from agentic_memory.embed import Embedder
from agentic_memory.metrics import REGISTRY

SESSION = "11111111-2222-3333-4444-555555555555"
ASK = {"session_id": SESSION, "harness": "pi", "scope_key": "example.test/acme/widget"}


@pytest.fixture
async def service(store: asyncpg.Pool, embedder: Embedder) -> AsyncIterator[str]:
    """The service on a port of its own, with one statement to hand out."""
    await store.execute(
        """
        INSERT INTO memories
            (statement, kind, score, scope_key, session_id, entry_id, model,
             questions_fingerprint, said_at, actor, actor_depth)
        VALUES ('Tests come before code here.', 'preference', 0.9, 'example.test/acme/widget',
                's1', 'e1',
                'jev-1.13.0', 'fp', now(), 'human', 0)
        """
    )
    app.state.pool = store
    app.state.embedder = embedder
    port = _postgres.free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)


@pytest.fixture
async def held(store: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """A lock on the injections table, which holds every recall at its first read.

    A recall reads what its session was handed before anything else, so a
    recall that arrives while this is held waits until the test commits.
    """
    connection = await store.acquire()
    await connection.execute("BEGIN; LOCK TABLE injections IN ACCESS EXCLUSIVE MODE")
    try:
        yield connection
    finally:
        if connection.is_in_transaction():
            await connection.execute("ROLLBACK")
        await store.release(connection)


async def rows(store: asyncpg.Pool) -> int:
    return await store.fetchval("SELECT count(*) FROM injections")


def recalls(form: str, outcome: str) -> float:
    found = REGISTRY.get_sample_value(
        "agentic_memory_recalls_total", {"form": form, "outcome": outcome}
    )
    return found or 0.0


async def counted(form: str, outcome: str, above: float) -> None:
    """Wait for the service to count a recall it finished after its client left."""
    async with asyncio.timeout(5):
        while recalls(form, outcome) <= above:
            await asyncio.sleep(0.01)


async def test_a_recall_whose_client_waits_is_recorded(service: str, store: asyncpg.Pool):
    async with httpx.AsyncClient(base_url=service, timeout=5) as client:
        answer = await client.post("/recall", json=ASK)
    assert len(answer.json()["statements"]) == 1
    assert await rows(store) == 1


async def test_a_recall_whose_client_left_is_not_recorded(service, store, held):
    before = recalls("opening", "gone")
    async with httpx.AsyncClient(base_url=service, timeout=0.3) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post("/recall", json=ASK)
    await held.execute("COMMIT")
    await counted("opening", "gone", before)
    assert await rows(store) == 0


@pytest.fixture
async def scraped(service: str) -> AsyncIterator[str]:
    """The scrape after a session's first ask and its second."""
    async with httpx.AsyncClient(base_url=service, timeout=5) as client:
        await client.post("/recall", json=ASK)
        await client.post("/recall", json={**ASK, "prompt": "what rhymes with orange?"})
        yield (await client.get("/metrics")).text


@pytest.mark.parametrize(
    "phase", ["seen", "opening", "record", "embed", "neighbours", "nearest", "request"]
)
async def test_a_scrape_has_the_time_of_each_phase(scraped: str, phase: str):
    assert f'agentic_memory_recall_seconds_count{{phase="{phase}"}}' in scraped


@pytest.mark.parametrize(
    "form, outcome",
    [("opening", "handed"), ("match", "nothing")],
)
async def test_a_scrape_counts_recalls_by_form_and_outcome(scraped: str, form: str, outcome: str):
    assert f'agentic_memory_recalls_total{{form="{form}",outcome="{outcome}"}}' in scraped


async def test_a_scrape_keeps_the_counts_of_the_store(scraped: str):
    assert "agentic_memory_memories_live 1" in scraped


async def test_the_misses_a_client_reports_are_counted(service: str):
    before = REGISTRY.get_sample_value("agentic_memory_recall_deadline_misses_total")
    async with httpx.AsyncClient(base_url=service, timeout=5) as client:
        await client.post("/recall", json={**ASK, "missed": 3})
    after = REGISTRY.get_sample_value("agentic_memory_recall_deadline_misses_total")
    assert after - before == 3
