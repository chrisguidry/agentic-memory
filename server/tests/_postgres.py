"""A Postgres container for the tests, one per pytest worker.

Every test that reads or writes SQL runs against a real server, because the
queries are where the bugs have been and a fake store cannot find one. The
container is the suite's own: it takes a free port the kernel hands out and a
name nobody else uses, so any number of sessions can run the suite on one
machine at once and none of them touches the compose stack on 5433.
"""

import contextlib
import socket
import time
from datetime import UTC, datetime, timedelta

import docker
from docker import DockerClient
from docker.models.containers import Container

# pgvector's build, the same one the compose stack runs, because the schema
# creates the extension.
IMAGE = "pgvector/pgvector:0.8.6-pg17-trixie"
USER = "agentic_memory"
PASSWORD = "agentic_memory"
SOURCE_LABEL = "agentic-memory-tests"

# A container this old was left by a run that did not get to stop it, and no
# run lasts this long.
STALE = timedelta(minutes=15)


def free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


def remove_stale(client: DockerClient) -> None:
    """Remove the containers earlier runs left behind."""
    now = datetime.now(UTC)
    for container in client.containers.list(all=True, filters={"label": f"source={SOURCE_LABEL}"}):
        created = container.attrs.get("Created", "")
        if not created:
            continue
        # Docker gives nanoseconds, which fromisoformat refuses.
        started = datetime.fromisoformat(created.split(".")[0]).replace(tzinfo=UTC)
        if now - started > STALE:
            # Another run may have removed it first.
            with contextlib.suppress(docker.errors.APIError):
                container.remove(force=True)


def start(client: DockerClient, label: str) -> tuple[Container, int]:
    """Run a server and wait until it takes connections over TCP.

    The image's entrypoint runs a first server on a Unix socket to initialize
    the cluster and then restarts it on the network, so readiness is asked over
    TCP or the first server answers for the second.
    """
    port = free_port()
    container = client.containers.run(
        IMAGE,
        detach=True,
        auto_remove=True,
        ports={"5432/tcp": ("127.0.0.1", port)},
        environment={"POSTGRES_USER": USER, "POSTGRES_PASSWORD": PASSWORD},
        labels={"source": SOURCE_LABEL, "container_label": label},
        # Nothing here outlives the run, so durability buys nothing.
        command=["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off"],
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = container.exec_run(["pg_isready", "-h", "127.0.0.1", "-U", USER])
        if ready.exit_code == 0:
            return container, port
        time.sleep(0.1)
    container.stop()
    raise RuntimeError(f"Postgres on port {port} did not become ready in 60s")


def url(port: int, database: str) -> str:
    return f"postgresql://{USER}:{PASSWORD}@127.0.0.1:{port}/{database}"
