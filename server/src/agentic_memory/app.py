"""The service.

It receives OTLP over HTTP, writes what arrived to the raw table, unpacks it
into the queryable one, and hands every prompt that arrived to the classifier.
It derives nothing itself, and it calls no model.
"""

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from docket import Docket
from fastapi import FastAPI, Query, Request

from . import db
from . import memories as statements
from . import synthesize as writer
from .classify import (
    KIND_COLUMNS,
    classify,
    readable_prompts,
    statement_key,
    task_key,
    worth_reading,
)
from .db import store_pool
from .otlp import walk
from .settings import get_settings

log = logging.getLogger("agentic_memory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        app.state.pool = await stack.enter_async_context(store_pool())
        # The schema is applied here and not by the worker, so two processes
        # starting together cannot run the same data definition at once.
        await db.apply_schema(app.state.pool)
        # The service schedules work and never runs it, so a model call cannot
        # hold up an ingest. The worker holds the same docket and reads there.
        app.state.docket = await stack.enter_async_context(
            Docket(name=settings.docket_name, url=settings.redis_url)
        )
        log.info("connected to the store")
        yield


app = FastAPI(title="agentic-memory", lifespan=lifespan)


async def schedule(docket: Docket, prompts: list[tuple[str, str]]) -> int:
    """Hand every prompt that arrived to the classifier.

    The key names the entry and the question set, so a record that arrives twice
    schedules one reading and a re-read under new questions is its own work.
    Scheduling never fails the request: the record is already stored, and a
    prompt that goes unread can be read again from the raw table.
    """
    scheduled = 0
    for session_id, entry_id in prompts:
        try:
            await docket.add(classify, key=task_key(session_id, entry_id))(session_id, entry_id)
            scheduled += 1
        except Exception:
            log.warning("could not schedule %s %s", session_id, entry_id, exc_info=True)
    return scheduled


@app.get("/health")
async def health(request: Request) -> dict:
    """Report whether the store answers, and how much it holds."""
    return {"status": "ok", **await db.count(request.app.state.pool)}


@app.post("/v1/logs")
async def logs(request: Request) -> dict:
    """The OTLP/HTTP endpoint for the logs signal."""
    payload = await request.json()
    arrived = list(walk(payload))
    stored = await db.store(request.app.state.pool, arrived)
    await schedule(request.app.state.docket, worth_reading(stored.prompts))
    return {"partialSuccess": {}, **stored.counted}


@app.get("/records")
async def records(
    request: Request,
    scope_key: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """The most recent records, for looking at what arrived."""
    return await db.recent(
        request.app.state.pool,
        scope_key=scope_key,
        kind=kind,
        session_id=session_id,
        harness=harness,
        limit=limit,
    )


@app.get("/classifications")
async def classifications(
    request: Request,
    scope_key: str | None = None,
    session_id: str | None = None,
    above: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """What the classifier read, above a probability the caller chooses.

    The threshold is the caller's, because the reading stores a probability
    rather than a decision about it.
    """
    return await db.classified(
        request.app.state.pool,
        kinds=KIND_COLUMNS,
        scope_key=scope_key,
        session_id=session_id,
        above=above,
        limit=limit,
    )


@app.post("/reread")
async def reread(
    request: Request,
    since: datetime,
    until: datetime | None = None,
    harness: str | None = None,
    limit: int = Query(500, ge=1, le=10000),
) -> dict:
    """Read messages that happened in a range, under the current questions.

    This is how the questions get calibrated. Change one, read the last few
    days again, and compare the answers. The readings taken under the questions
    before it stay where they are, named by their own fingerprint.
    """
    prompts = await readable_prompts(
        request.app.state.pool,
        since=since,
        until=until or datetime.now(UTC),
        harness=harness,
        limit=limit,
    )
    return {
        "candidates": len(prompts),
        "scheduled": await schedule(request.app.state.docket, prompts),
    }


@app.post("/write")
async def write(request: Request) -> dict:
    """Write statements for every reading that is still worth one.

    The writing normally follows a reading on its own. This runs it over the
    readings already stored, which is what makes the second pass tunable: its
    thresholds can move and the statements be written again from the same
    readings.
    """
    wanted = await writer.worth_writing(request.app.state.pool)
    for session_id, entry_id in wanted:
        try:
            await request.app.state.docket.add(
                writer.synthesize, key=statement_key(session_id, entry_id)
            )(session_id, entry_id)
        except Exception:
            log.warning("could not schedule writing %s %s", session_id, entry_id, exc_info=True)
    return {"candidates": len(wanted)}


@app.get("/memories")
async def memories(
    request: Request,
    scope_key: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """What is worth remembering, for a place.

    Retrieval walks up the scope path, so a statement about a repository is
    reachable from any directory in it. A statement scoped to nothing is
    reachable from everywhere. What comes back is ordered by the ranking, which
    weighs the kind and the age of each statement, and a statement a newer one
    replaced is not in it.
    """
    return await statements.memories(request.app.state.pool, scope_key=scope_key, limit=limit)


@app.post("/rebuild")
async def rebuild(request: Request) -> dict:
    """Throw the unpacked table away and build it again from the raw one.

    This is what the raw table exists for. The extraction can change and the
    record does not have to be sent again.
    """
    return await db.rebuild(request.app.state.pool)


def main() -> None:
    """Run the service."""
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
