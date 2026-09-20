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
| `tools/backfill.py` | The host, through `uv` | Reads the session files a harness already wrote |
| `tools/harnesses/` | The host | One module per harness, which is the only place a format is read |
| `tools/scope.py` | The host | Derives a session's scope from the directories it is in |
| `docker-compose.yml` | The host | Postgres, and the server |

A transcript record is a log record. Its body holds the text, and its
attributes hold the provenance. Every attribute name comes from
OpenTelemetry, and [`docs/otel-conventions.md`](docs/otel-conventions.md)
lists them with their sources.

Each record carries a scope, which says what the session was about:
`github.com/acme/widget` for a repository, `github.com/acme` for the
organization above it. The client derives the scope from the directories
the session is in, and [`plans/00-design.md`](plans/00-design.md) holds the
scheme.

## Running it

```bash
docker compose up -d --build          # Postgres, then the server
docker compose logs -f server         # watch it work

ln -sfn "$PWD/pi" ~/.pi/agent/extensions/agentic-memory

uv run tools/backfill.py --harness pi
uv run tools/backfill.py --harness claude-code
uv run tools/backfill.py --harness codex
```

The service listens on `127.0.0.1:4318`, which is the OTLP/HTTP port. The
source is mounted into the container and uvicorn reloads it, so an edit to
`server/src` takes effect without a rebuild.

```bash
curl -s http://127.0.0.1:4318/health
curl -s "http://127.0.0.1:4318/records?scope=github.com/acme&limit=5"
```

## What a backfill produces

`tools/backfill.py` reads the session files a harness already wrote and
sends them as OTLP log records. A run over several months of one machine's
sessions produced records carrying:

- the session and the entry inside it, which make a re-send harmless
- the scope, derived from the directory the session started in
- the model, the tokens, and the cost of each response
- the chain back to the person, for a session a subagent ran
- the session's title and its cost totals, where the harness recorded them

A record that arrives twice is stored once, because the entry id is the
same from every producer. Re-running the backfill over sessions the live
hook already captured adds nothing.

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
backfill as a separate tool, and it reaches a harness through a shim rather
than through a backfill that reads what the harness already wrote.

The store keeps the record four ways: the export as it arrived, the unpacked
log a query reads, and a table each for the resource and the scope that every
OpenTelemetry signal carries. The export is the permanent copy, so the
unpacked form can be thrown away and rebuilt when the extraction changes.
