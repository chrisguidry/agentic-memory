"""The service.

It receives OTLP over HTTP, writes what arrived to the raw table, unpacks it
into the queryable one, and hands every prompt that arrived to the classifier.
It derives nothing itself, and it calls no model.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import uvicorn
from docket import Docket
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import db, embed, ingest, ledger, recall
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
from .merge import merge_statements
from .otlp import walk
from .settings import get_settings
from .transcripts import Chunk, export

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
        # The model that embeds a prompt, loaded once. It takes a second to load
        # and twelve milliseconds a prompt, and the turn path waits on the second.
        app.state.embedder = await asyncio.to_thread(embed.load, settings)
        log.info("connected to the store")
        yield


app = FastAPI(title="agentic-memory", lifespan=lifespan)


async def schedule(docket: Docket, prompts: list[tuple[str, str]], run: str = "live") -> int:
    """Hand every prompt that arrived to the classifier.

    The key names the entry and the question set, so a record that arrives twice
    schedules one reading and a re-read under new questions is its own work.
    Scheduling never fails the request: the record is already stored, and a
    prompt that goes unread can be read again from the raw table.

    The run travels with the scheduled task, so every call the task makes and
    every call the writing after it makes is recorded under the same name.
    """
    scheduled = 0
    for session_id, entry_id in prompts:
        try:
            await docket.add(classify, key=task_key(session_id, entry_id))(
                session_id, entry_id, run
            )
            scheduled += 1
        except Exception:
            log.warning("could not schedule %s %s", session_id, entry_id, exc_info=True)
    return scheduled


@app.get("/health")
async def health(request: Request) -> dict:
    """Report whether the store answers, and how much it holds."""
    return {"status": "ok", **await db.count(request.app.state.pool)}


@app.get("/metrics")
async def metrics(request: Request) -> PlainTextResponse:
    """The numbers Prometheus scrapes, in its text format.

    A scrape reads the store, so the numbers are current rather than counted in
    the process, and a PodMonitor over this endpoint is the whole metrics side.
    """
    found = await db.metrics(request.app.state.pool)
    lines: list[str] = []
    for name, value in found["gauges"].items():
        lines.append(f"# TYPE agentic_memory_{name} gauge")
        lines.append(f"agentic_memory_{name} {value}")
    lines.append("# TYPE agentic_memory_model_calls counter")
    for task, calls in found["calls"]:
        lines.append(f'agentic_memory_model_calls{{task="{task}"}} {calls}')
    return PlainTextResponse("\n".join(lines) + "\n")


@app.post("/v1/logs")
async def logs(request: Request) -> dict:
    """The OTLP/HTTP endpoint for the logs signal."""
    payload = await request.json()
    arrived = list(walk(payload))
    stored = await ingest.store(request.app.state.pool, arrived)
    await schedule(request.app.state.docket, worth_reading(stored.prompts))
    return {"partialSuccess": {}, **stored.counted}


@app.post("/v1/transcripts")
async def transcripts(request: Request, chunk: Chunk) -> dict:
    """The endpoint a client posts transcript lines to.

    The client sends bytes and never reads a format. The service parses the
    lines with the harness's own reader and stores what they hold the same way
    `/v1/logs` stores an export a client parsed for itself.
    """
    try:
        arrived = list(walk(export(chunk)))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    stored = await ingest.store(request.app.state.pool, arrived)
    await schedule(request.app.state.docket, worth_reading(stored.prompts))
    return stored.counted


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
    run: str = "reread",
    limit: int = Query(500, ge=1, le=10000),
) -> dict:
    """Read messages that happened in a range, under the current questions.

    This is how the questions get calibrated. Change one, read the last few
    days again, and compare the answers. The readings taken under the questions
    before it stay where they are, named by their own fingerprint.

    The run names the backfill, and it travels into every call the reading and
    the writing after it make, so the calls of one backfill total together.
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
        "scheduled": await schedule(request.app.state.docket, prompts, run),
    }


@app.post("/write")
async def write(request: Request, run: str = "write") -> dict:
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
            )(session_id, entry_id, run)
        except Exception:
            log.warning("could not schedule writing %s %s", session_id, entry_id, exc_info=True)
    return {"candidates": len(wanted)}


@app.get("/memories")
async def memories(
    request: Request,
    scope_key: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    order: Literal["rank", "newest"] = "rank",
) -> list[dict]:
    """What is worth remembering, for a place.

    Retrieval walks up the scope path, so a statement about a repository is
    reachable from any directory in it. A statement scoped to nothing is
    reachable from everywhere. `rank` orders what a turn is handed, which weighs
    the kind and the age of each statement. `newest` orders what the writer last
    produced. A statement a newer one replaced is in neither.
    """
    return await statements.memories(
        request.app.state.pool, scope_key=scope_key, limit=limit, order=order
    )


class Ask(BaseModel):
    """What a client says about the turn that is starting."""

    session_id: str
    harness: str
    scope_key: str | None = None
    # What the person typed, so a turn after the first can be handed what is
    # about it. Empty means the session's first ask is the only form served.
    prompt: str = ""
    limit: int = Field(recall.LIMIT, ge=1, le=recall.LIMIT)


@app.post("/recall")
async def recall_for_turn(request: Request, ask: Ask) -> dict:
    """What a turn is handed.

    A session's first ask gets the top of its scope's list. Every ask after it
    gets the statements that are about the prompt, or nothing. Both leave out
    what this session was already handed, and both are recorded, so the outcome
    flow has something to join to.
    """
    found = await recall.turn(
        request.app.state.pool,
        request.app.state.embedder,
        get_settings(),
        session_id=ask.session_id,
        harness=ask.harness,
        scope_key=ask.scope_key,
        prompt=ask.prompt,
    )
    return {
        "statements": [
            {
                "id": row["id"],
                "statement": row["statement"],
                "kind": row["kind"],
                "scope_key": row["scope_key"],
                "said_at": row["said_at"].isoformat() if row["said_at"] else None,
                "actor": row["actor"],
                "actor_depth": row["actor_depth"],
            }
            for row in found
        ]
    }


@app.get("/injections")
async def injections(
    request: Request, session_id: str, limit: int = Query(50, ge=1, le=500)
) -> list[dict]:
    """What a session was handed, latest turn first, for watching the experiment."""
    return await recall.handed(request.app.state.pool, session_id=session_id, limit=limit)


@app.post("/embed")
async def embed_all(request: Request) -> dict:
    """Embed every live statement the model has not, which applies a model change."""
    await request.app.state.docket.add(embed.embed_statements, key="embed-statements")()
    return {"scheduled": True}


@app.post("/merge")
async def merge_all(request: Request, run: str = "merge") -> dict:
    """Merge the near-duplicates already in the table, oldest statement first.

    A statement is merged as it is written after this. This pass is for what
    was written before the merge existed, and for a model change that re-embeds
    the table. The run names the pass, so its calls total under one name.
    """
    await request.app.state.docket.add(merge_statements, key="merge-statements")(run)
    return {"scheduled": True}


@app.get("/usage")
async def usage(
    request: Request,
    by: Literal["day", "model", "task", "run"] = "model",
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """What the worker's model calls cost, totalled four ways.

    A total alone cannot separate a deep backfill from ongoing use, so every
    call records the run it belongs to and the model that answered it. The
    totals carry no dollars, because a price changes and a token count does
    not; the exact model stays on every row so a later price can be applied.
    """
    return await ledger.usage(request.app.state.pool, by=by, since=since, until=until)


@app.get("/model_calls")
async def model_calls(
    request: Request,
    since: datetime | None = None,
    until: datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: str | None = None,
    run: str | None = None,
    session_id: str | None = None,
    outcome: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """The raw model calls, newest first, for reading the figures themselves.

    This is the read an agent pulls when a total is not enough. It narrows by
    any field, and `next` carries the cursor that continues where the page
    ended, so a page never repeats a row while new calls arrive. Nothing is
    aggregated.
    """
    try:
        found = await ledger.calls(
            request.app.state.pool,
            since=since,
            until=until,
            provider=provider,
            model=model,
            task=task,
            run=run,
            session_id=session_id,
            outcome=outcome,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "calls": found,
        "next": ledger.cursor_of(found[-1]) if len(found) == limit else None,
    }


@app.post("/rebuild")
async def rebuild(request: Request) -> dict:
    """Throw the unpacked table away and build it again from the raw one.

    This is what the raw table exists for. The extraction can change and the
    record does not have to be sent again.
    """
    return await ingest.rebuild(request.app.state.pool)


def main() -> None:
    """Run the service."""
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
