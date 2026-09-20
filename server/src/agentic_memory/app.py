"""The service.

It receives OTLP over HTTP and writes what arrives to Postgres. It derives
nothing, and it calls no model.
"""

import logging
from contextlib import asynccontextmanager

import asyncpg
import uvicorn
from fastapi import FastAPI, Query, Request

from . import db
from .otlp import row, walk
from .settings import get_settings

log = logging.getLogger("agentic_memory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    log.info("connected to the store")
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="agentic-memory", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict:
    """Report whether the store answers, and how much it holds."""
    return {"status": "ok", "records": await db.count(request.app.state.pool)}


@app.post("/v1/logs")
async def logs(request: Request) -> dict:
    """The OTLP/HTTP endpoint for the logs signal."""
    payload = await request.json()
    rows = [row(resource, scope, record) for resource, scope, record in walk(payload)]
    inserted, repeated, failed = await db.store(request.app.state.pool, rows)
    return {
        "partialSuccess": {},
        "inserted": inserted,
        "repeated": repeated,
        "failed": failed,
    }


@app.get("/records")
async def records(
    request: Request,
    project: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """The most recent records, for looking at what arrived."""
    return await db.recent(
        request.app.state.pool,
        project=project,
        kind=kind,
        session_id=session_id,
        harness=harness,
        limit=limit,
    )


def main() -> None:
    """Run the service."""
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
