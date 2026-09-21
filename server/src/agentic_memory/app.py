"""The service.

It receives OTLP over HTTP, writes what arrived to the raw table, unpacks it
into the queryable one, and hands every prompt that arrived to the classifier.
It derives nothing itself, and it calls no model.
"""

import logging
from contextlib import AsyncExitStack, asynccontextmanager

import asyncpg
import uvicorn
from docket import Docket
from fastapi import FastAPI, Query, Request

from . import db
from .classify import classify
from .otlp import walk
from .settings import get_settings

log = logging.getLogger("agentic_memory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        app.state.pool = await stack.enter_async_context(
            asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
        )
        # The service schedules work and never runs it, so a model call cannot
        # hold up an ingest. The worker holds the same docket and reads there.
        app.state.docket = await stack.enter_async_context(
            Docket(name=settings.docket_name, url=settings.redis_url)
        )
        log.info("connected to the store")
        yield


app = FastAPI(title="agentic-memory", lifespan=lifespan)


async def schedule(docket: Docket, prompts: list[tuple[str, str]]) -> None:
    """Hand every prompt that arrived to the classifier.

    The key names the entry, so a record that arrives twice schedules one
    reading. Scheduling never fails the request: the record is already stored,
    and a prompt that goes unread can be read again from the raw table.
    """
    for session_id, entry_id in prompts:
        try:
            await docket.add(classify, key=f"classify:{session_id}:{entry_id}")(
                session_id, entry_id
            )
        except Exception:
            log.warning("could not schedule %s %s", session_id, entry_id, exc_info=True)


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
    await schedule(request.app.state.docket, stored.prompts)
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
    above: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """What the classifier read, above a probability the caller chooses.

    The threshold is the caller's, because the reading stores a probability
    rather than a decision about it.
    """
    return await db.classified(
        request.app.state.pool,
        scope_key=scope_key,
        above=above,
        limit=limit,
    )


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
