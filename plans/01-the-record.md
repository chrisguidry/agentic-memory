# 01, The record

## The problem

Every coding agent writes its sessions to disk on the machine that ran
it, in its own format, and nothing collects them. The person cannot
search what they and their agents have said, and no later plan can
derive memory from it.

This plan builds the warehouse and nothing else. It is useful on its own
as an archive, and every later plan reads from it.

## The flow

Capture happens at the hook. The client is the fallback, not the path.

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  the machine                                                     │
  │                                                                  │
  │   a turn happens                                                 │
  │       │                                                          │
  │       ├──► the harness's hook ──► a shim ──► OTLP ──┐            │
  │       │                                            │            │
  │       └──► the harness's own telemetry ──► OTLP ───┤            │
  │                                                    │            │
  │   on a timer, and at session start:                │            │
  │       │                                            │            │
  │       ▼                                            │            │
  │   the client reads the session files ──► OTLP ─────┘            │
  │                                                                  │
  └────────────────────────────────────┬─────────────────────────────┘
                                       │  OTLP
                                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  the collector  ──►  the service                                 │
  │                                                                  │
  │   accepting: separate by scope, resolve the machine and the      │
  │   session, drop a repeat, write the record                       │
  └──────────────────────────────────────────────────────────────────┘
```

### Live capture

A harness's hook fires, and a shim inside the harness builds one record
and emits it.

The hook hands the shim the content in the harness's own structure, so
the shim parses no file and knows no file format. It fills the
provenance from what the hook gives it and from its own configuration,
and it sends the record.

This is the primary path because a harness's session file is a private
format that changes between releases. A hook hands over the content
directly, so the shim never depends on that format.

Every harness gets a shim, including the harnesses that export
OpenTelemetry of their own. A harness's export is a second schema to
translate and a second thing to keep in step with the record. One shim
per harness keeps one record shape, and Claude Code's export stays
useful for the numbers it carries and for nothing else.

### The sweep

A timer fires, or a session starts. The client reads the session files
each harness wrote and ships what it has not confirmed.

The sweep is what makes the record complete. It catches a session whose
hook never fired, a harness whose hook was never installed, a record a
shim dropped, and a harness that has no hook at all. It is the only path
for Codex.

The sweep is also the only place a harness file format is read, and that
is deliberate. A fallback reads whatever the harness left behind.

### The backfill

The first run reads everything a machine already holds. The measured
volume is 2.4 GB on one machine against 92 MB per day, so the backfill
is larger than a year of steady capture.

The backfill is the sweep with no offset, run once and paced.

### A source other than this machine

The same command reads a source elsewhere: a directory of session files,
a mounted copy of a home directory, or an archive. That is what a person
needs when they set the system up, or when they retire a machine that
holds sessions the service never saw.

```
memory sweep --machine <name> --from <path>
```

The machine is named, never inferred. A backfill of a desktop's
transcripts, run from a laptop, records the desktop. Without that rule
the provenance is wrong in a way nothing later can detect, and the trust
model reads the provenance. A record that names the wrong machine is
worse than one with no machine, because it looks right.

The source is read and never written. Running the command twice costs
bandwidth and stores nothing twice, because the entry id is the same.

### Accepting

The collector receives the batch and forwards it to the service, which
is the only writer.

The service separates a record from a harness's own telemetry by
instrumentation scope. For a record it resolves the machine and the
session, drops anything it already holds, and writes what remains.

A confirmation is what lets the client advance an offset. A shim ignores
it, because the sweep is what guarantees the record and the shim only
makes it early.

## What makes the flow hard

### The hook can miss a session

A hook does not fire when a process is killed, when a harness is
upgraded, or when the hook was never installed. A session lost that way
is lost for good, because nothing else would have kept it.

That is why the sweep exists, and why it is not optional. The hook makes
the record early. The sweep makes the record complete.

### A shim runs inside the agent

The shim runs in the harness's own process, so a shim that raises takes
down a turn. It must be small, it must never throw, and it must treat a
missing collector as ordinary.

A shim that cannot reach the collector drops the record and returns. The
sweep finds it later.

### A hook can stall the session

Claude Code blocks the turn until a `UserPromptSubmit` hook returns, and
its default timeout is 30 seconds. A hook that waits on a slow collector
stalls the conversation.

The shim never waits on the collector. It hands the record to the SDK
and returns, and the SDK sends it without holding the turn. The
injection path is the one that waits, and it waits no longer than 150 ms.

### The record can arrive twice

A shim ships a record as it happens, and the sweep ships the same record
from the file. The service drops the second copy on the entry id.

The cost is bandwidth on the wire. The benefit is that the record never
depends on a shim behaving, and a shim that fails silently costs nothing
but time.

### A harness rewrites its own file

Compaction replaces the early part of a session with a summary, so the
file shrinks and its content changes. An offset into it then points at
nothing.

The client stores a fingerprint beside each offset. When a file is
shorter than its offset, or the bytes at the offset do not match the
fingerprint, the client reads from the start again. The entry id makes
the re-read harmless.

### A machine is offline for a long time

Nothing is lost. The client advances an offset only on a confirmation,
so a laptop closed for a week ships the whole week on its next run.

### The first run ships everything

The backfill is far larger than a day of capture. The client paces it
and reports progress, so a person sees a backfill with a size and an
estimate instead of a hang.

## The shape

### The service

A FastAPI application over Postgres, behind an OTLP receiver. It accepts
records, accepts a harness's own telemetry, and answers questions about
what it holds.

It does not call a model, and nothing in this plan derives anything.

### The client

One Go binary per platform, with three jobs.

- **Sweep.** Read what each harness wrote, send what the service has not
  seen, and return.
- **Install.** Write a harness's own hook configuration, or a timer for
  a harness that has none.
- **Status.** Report what is unsent and whether the service answers.

### The shims and the SDK

One shim per harness, and each shim is a few lines of glue between a
hook and an SDK. The SDK carries the record shape, the batching, the
retries, and the deadline, so a shim holds no protocol.

The SDK appears here because live capture needs it, and the injection
path reuses it.

| Language | For |
|---|---|
| TypeScript | pi and OpenCode, which run their extensions in Node |
| Python | OpenWebUI, and any agent written in Python |

### The adapters

One Go package per harness, and the adapter owns every harness-specific
decision. An adapter answers two questions: which files on this machine
belong to this harness, and what is in one of them.

The adapters serve the sweep and the backfill. The shims never use them.

### The provenance

Every record carries the machine, the harness, the session, the parent
session, the actor, the actor's depth, the root, the model, the kind,
and the working directory.

The actor is the field that matters. It is a person, an agent, or the
harness itself. A prompt the person typed is depth zero. A prompt an
orchestrator wrote for a subagent is depth one.

A subagent's prompt arrives in a harness's log as a user message. An
adapter that records it as the person's own words teaches the extraction
pass a preference the person never stated, and nothing later can tell
the difference.

The chain also records where it starts. A session a person began has a
person at its root. A session an autonomous agent began has no person in
it at all. Both are an agent at some depth, and they mean opposite
things, so the root is recorded rather than inferred later.

## What the plan does not do

- No model is called, and nothing is derived.
- No memory exists, and no blackboard exists.
- No redaction. The record holds what the person pasted, and the store
  is private.

## The proof

A drill against a local stack, with pi and Claude Code on one machine.

1. Run one session in each harness. Each session asks a question, reads
   a file, and writes a file.
2. Confirm each session appears in the service while the session is
   still running, which is what proves the hook path rather than the
   sweep.
3. Read each session back and confirm the machine, harness, session,
   parent session, actor, depth, root, model, and working directory on
   every record.
4. Run a session that spawns a subagent. Confirm the subagent's session
   names its parent, and that its prompts carry an agent actor at a
   depth above zero.
5. Stop the collector, run a session, and confirm the session is not
   slowed and does not fail. Start the collector and confirm the sweep
   delivers what the shim dropped.
6. Kill a session with a signal so no hook fires, then sweep. Confirm
   the session appears and that the record count matches what the hook
   path produced for the same work.
7. Sweep twice more and confirm the record count does not change.
8. Truncate a session file the way a compaction would, sweep again, and
   confirm the record count still does not change.
9. Search for a string one session contains.
10. Copy one machine's session files to a second machine, sweep with
    `--machine` naming the first, and confirm every record carries the
    first machine's name and that the source is unchanged.

The drill records the bytes it ingested, the time each session took to
appear by each path, and the time the first backfill took.
