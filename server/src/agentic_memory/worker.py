"""Running the tasks that read the record.

The worker is a separate process from the service. The service schedules work
and never runs it, so a slow model call cannot hold up an ingest.
"""

import asyncio
import logging

from docket import Docket, Worker

from .classify import classify
from .db import apply_schema, open_pool
from .embed import embed_statements
from .merge_pass import merge_statements
from .readings import embed_reading
from .settings import get_settings
from .sweep import sweep_failures
from .synthesize import synthesize

log = logging.getLogger("agentic_memory.worker")

# Every task the worker can run. A docket holds a name rather than a
# reference, so a task that is not here is one the worker cannot run.
#
# sweep_failures is Perpetual and automatic: registering it here is what
# schedules it, at startup and again if its chain is ever lost, with no call
# to docket.add of its own.
TASKS = (
    classify,
    synthesize,
    embed_statements,
    embed_reading,
    merge_statements,
    sweep_failures,
)


async def prepare_store(database_url: str) -> None:
    """Bring the store up to the schema this code needs, before any task runs.

    The service and the worker roll out separately, and a worker that starts
    first would run every task against the store the old code left.
    """
    pool = await open_pool(database_url, size=1)
    try:
        await apply_schema(pool)
    finally:
        await pool.close()


async def serve() -> None:
    """Run a worker until it is stopped."""
    settings = get_settings()
    await prepare_store(settings.database_url)
    async with Docket(name=settings.docket_name, url=settings.redis_url) as docket:
        for task in TASKS:
            docket.register(task)
        async with Worker(docket) as worker:
            log.info("worker %s is reading the record", worker.name)
            await worker.run_forever()


def main() -> None:
    """Run the worker."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
