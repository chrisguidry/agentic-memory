"""Running the tasks that read the record.

The worker is a separate process from the service. The service schedules work
and never runs it, so a slow model call cannot hold up an ingest.
"""

import asyncio
import logging

from docket import Docket, Worker

from .classify import classify
from .settings import get_settings
from .synthesize import synthesize

log = logging.getLogger("agentic_memory.worker")

# Every task the worker can run. A docket holds a name rather than a
# reference, so a task that is not here is one the worker cannot run.
TASKS = (classify, synthesize)


async def serve() -> None:
    """Run a worker until it is stopped."""
    settings = get_settings()
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
