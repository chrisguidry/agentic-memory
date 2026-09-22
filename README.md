# agentic-memory

Keeps one person's memories across every coding agent and every machine
they use.

This repository holds the design in [`plans/`](plans/) and the service it
describes. The service records what a person and their agents said, as
OpenTelemetry log records, with enough provenance to work from later. A
worker reads that record off the turn path: a cheap model classifies each
message, a writer turns the ones worth keeping into statements, a merge
retires the ones that say the same thing, and a ledger records every model
call the worker makes.

## What runs

| Piece | Where | What it does |
|---|---|---|
| `server/` | Docker Compose | Receives OTLP on `/v1/logs`, writes it to Postgres, and answers recall |
| `server/` (worker) | Docker Compose | Reads the record and calls the models, off the turn path |
| `pi/` | A symlink into `~/.pi/agent/extensions/` | Sends each message as it happens, and asks for memory before each turn |
| `tools/hook.py` | A Claude Code hook | Sends what a transcript gained on each event |
| `tools/backfill.py` | The host, through `uv` | Reads the session files a harness already wrote |
| `tools/harnesses/` | The host | One module per harness, which is the only place a format is read |
| `tools/scope.py` | The host | Derives a session's scope from the directories it is in |
| `docker-compose.yml` | The host | Postgres, Redis, the server, and the worker |

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

## The image

CI builds `ghcr.io/chrisguidry/agentic-memory` and pushes it on every push
to main. The version is calver, the scheme the liken repositories use. A
release tag is `YYYY.MM.DD-NNN`, and a development build names the release
it follows, the commits since it, and the commit, as
`YYYY.MM.DD-NNN-dev-CCC-SHA`. A release tag moves `latest`; a development
build carries only its own version.

The production image bakes the source, the schema, and the embedding model,
and runs as a non-root user, so a pod answers its first recall without
downloading anything. The compose file builds the `dev` target instead,
which mounts the source and reloads it.

```bash
docker build --target prod -t agentic-memory:prod server
```

## Deploying it

`deploy/` holds a kustomize base that runs the api and the worker, and three
optional components: a PodMonitor with a Grafana dashboard, an in-cluster
Postgres with pgvector, and an in-cluster Redis. The base reads a Secret and
creates no namespace, so a consumer supplies both. [`deploy/README.md`](deploy/README.md)
lists the Secret's keys and shows an overlay that points at the base.

## Live capture from Claude Code

`tools/hook.py` is a Claude Code hook. On `UserPromptSubmit`, `Stop`,
`SubagentStop`, and `SessionEnd` it reads what the session's transcript
gained since the last event and sends it through the same reader the
backfill uses, so a record sent live and the same record swept later
carry one entry id and the service keeps one copy.

The foreground exits in under a tenth of a second and never writes to
stdout, because a `UserPromptSubmit` hook's stdout goes into the
conversation. The send runs in a detached process. Each transcript's byte
offset, entry count, and head fingerprint live in one file under
`~/.local/state/agentic-memory/claude-code/`, beside `hook.log`, which is
the only place a failure is written. A transcript that shrinks or whose
head changes is sent again from the start, and the service drops the
repeats.

Register it under each of the four events in `~/.claude/settings.json`:

```json
{"type": "command", "command": "python3 /path/to/agentic-memory/tools/hook.py", "timeout": 5}
```

## Recall into Claude Code

`tools/recall.py` is the other half of the hook. On `UserPromptSubmit` it
derives the scope from the working directory, sends the prompt and the
scope to the service, and hands what comes back to the turn as context in
the shape the hook docs specify. The pi extension does the same on
`before_agent_start`.

What comes back has two forms, and the service picks between them. A
session's first turn is handed the standing statements for the place, ten
by default, ranked by kind and age. Every turn after that is handed only
the statements that match the prompt, up to five, or nothing when none
matches well enough. In both forms the service leaves out what it already
handed this session. [`plans/completed/04-the-match.md`](plans/completed/04-the-match.md)
holds the match.

The deadline is 150 milliseconds in all, counted from the interpreter's
first line, and past it the turn proceeds with nothing. A missing service
and a slow one look the same from the turn. Nothing but the block is ever
written to stdout, because on this event stdout is context; failures go
to `recall.log` beside the capture hook's state. The statements are given
as the writer wrote them, one line each with kind, scope, who said it, and
age, so the raw list can be watched landing.

Register it under `UserPromptSubmit` in `~/.claude/settings.json`. The
`-S` matters: the whole budget is interpreter start.

```json
{"type": "command", "command": "python3 -S /path/to/agentic-memory/tools/recall.py", "timeout": 5}
```

`AGENTIC_MEMORY_RECALL_LIMIT` caps how many statements a turn is handed,
ten by default, and `AGENTIC_MEMORY_ENDPOINT` names the service.

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

- No blackboard exists. The store is read directly.
- pi and Claude Code are wired up live. OpenWebUI and the rest come later.
- The record holds whatever was in the transcript, secrets included. The
  store is private and redaction is not attempted.
- Extraction calls a model on every message and a larger one on the messages
  worth keeping, so the cost grows with the record. `GET /usage` totals what
  it has spent, and `GET /model_calls` lists the calls.

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
