# agentic-memory

Keeps one person's memories across every coding agent and every machine
they use.

This repository holds the design in [`plans/`](plans/) and a working proof
of concept that captures transcripts. The proof of concept does one thing:
it records what a person and their agents said, as OpenTelemetry log
records, with enough provenance to work from later. It derives nothing and
it calls no model.

## What runs

| Piece | Where | What it does |
|---|---|---|
| `server/` | Docker Compose | Receives OTLP on `/v1/logs` and writes it to Postgres |
| `pi/` | A symlink into `~/.pi/agent/extensions/` | Sends each message as it happens |
| `tools/backfill.py` | The host, through `uv` | Reads the session files pi already wrote |
| `docker-compose.yml` | The host | Postgres, and the server |

A transcript record is a log record. Its body holds the text, and its
attributes hold the provenance. Every attribute name comes from
OpenTelemetry, and [`docs/otel-conventions.md`](docs/otel-conventions.md)
lists them with their sources.

## Running it

```bash
docker compose up -d --build          # Postgres, then the server
docker compose logs -f server         # watch it work

ln -sfn "$PWD/pi" ~/.pi/agent/extensions/agentic-memory

uv run tools/backfill.py --from ~/.pi/agent/sessions
```

The service listens on `127.0.0.1:4318`, which is the OTLP/HTTP port. The
source is mounted into the container and uvicorn reloads it, so an edit to
`server/src` takes effect without a rebuild.

```bash
curl -s http://127.0.0.1:4318/health
curl -s "http://127.0.0.1:4318/records?project=liken-sh&limit=5"
```

## What is loaded

One machine's pi sessions, read on 2026-09-20.

| | |
|---|---|
| Records | 35,037 |
| Sessions | 228 |
| Projects | 12 |
| First and last | 2026-02-20 to 2026-09-20 |
| On disk | 218 MB |
| Tokens | 48.5M in, 6.5M out, 1.26B cache reads |
| Cost recorded | $276.77 |

| Kind | Records |
|---|---|
| `tool_result` | 13,334 |
| `response` | 12,082 |
| `thinking` | 8,357 |
| `prompt` | 1,257 |
| `system` | 7 |

Every record carries its session and its entry id. 84 sessions name a
parent, and every parent is in the store, so the chain from a subagent
back to the person resolves.

## What this is not

- No memory is derived, and no model is called.
- No blackboard exists.
- Only pi is wired up. Claude Code, OpenWebUI, and the rest come later.
- The record holds whatever was in the transcript, secrets included. The
  store is private and redaction is not attempted.

## Where the design is

[`plans/00-design.md`](plans/00-design.md) holds the architecture and the
flows. [`plans/01-the-record.md`](plans/01-the-record.md) is the plan this
code is a first cut of, at sketch fidelity.

The plan's version differs from this proof of concept in three ways worth
knowing. It sends records over OTLP from a Go client rather than from
Python, it sweeps session files as a safety net rather than treating the
backfill as a separate tool, and it keeps the record in two tables rather
than one wide one.
