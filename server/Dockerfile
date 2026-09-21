# The service, for local development.
#
# Dependencies are installed from the lock file in their own layer, so a
# change to the source does not reinstall them. The source is bind-mounted
# over the copy at run time and uvicorn reloads it, so an edit takes effect
# without a rebuild.
#
# Bytecode goes to /tmp, not beside the source. A run on the host writes
# __pycache__ into the tree the container mounts, and a stale entry there is
# read in preference to an edited file, which looks exactly like an edit that
# did nothing.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache

# The dependencies first, and the project itself never, because the source
# arrives as a mount and would only be shadowed by an installed copy.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

COPY src ./src

EXPOSE 4318

CMD ["uvicorn", "agentic_memory.app:app", "--host", "0.0.0.0", "--port", "4318"]
