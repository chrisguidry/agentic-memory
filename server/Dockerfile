# The service and the worker, in two shapes off one set of dependencies.
#
# `dev` is what the compose file builds: the source arrives as a mount and
# uvicorn reloads it, so an edit takes effect without a rebuild. Bytecode goes
# to /tmp, because a run on the host writes __pycache__ into the mounted tree
# and a stale entry there is read in preference to an edited file, which looks
# exactly like an edit that did nothing.
#
# `prod` is what CI builds and what a cluster runs: the source and the schema
# baked in, the embedding model already downloaded, and a non-root user. The
# model is baked because a recall is the one model-backed call on the turn
# path, and a pod that has to download 130 MB before it answers the first one
# is a pod that stalls a turn.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH

# The dependencies first, and the project itself never. The source is mounted
# in dev, so an installed copy would only shadow it. `db.py` reads the schema
# two directories above the package, which is /app this way, and a packaged
# install would move it under site-packages and break that.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM base AS dev

COPY src ./src

EXPOSE 4318

CMD ["uvicorn", "agentic_memory.app:app", "--host", "0.0.0.0", "--port", "4318"]

FROM base AS prod

ARG EMBED_MODEL=BAAI/bge-small-en-v1.5

ENV AGENTIC_MEMORY_EMBED_CACHE=/app/models

COPY src ./src
COPY schema.sql ./schema.sql

RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='$EMBED_MODEL', cache_dir='/app/models')"

RUN useradd --create-home --uid 10001 memory \
    && chown -R memory:memory /app

USER memory

EXPOSE 4318

CMD ["uvicorn", "agentic_memory.app:app", "--host", "0.0.0.0", "--port", "4318"]
