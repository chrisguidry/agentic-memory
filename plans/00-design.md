# Design

`agentic-memory` keeps one person's memories across every coding agent
and every machine they use. A client on each machine sends what the
person and their agents say to a service. The service stores it, derives
memory from it, and returns the memory that matters to an agent when
that agent starts work. The agent does not ask.

## The problem

Two things are lost today.

**Sessions are lost.** Every coding agent writes its sessions to disk on
the machine that ran it. A session on a laptop is invisible from a
workstation. A session from last month is invisible from today. The
person cannot search what they and their agents have said, and no later
work can derive memory from it.

**Context is lost.** An agent starts work with no knowledge of the
person's preferences, the decisions already taken, or the attempts
already failed. The person repeats themselves, and an agent repeats a
mistake that an earlier agent recorded and nobody read.

## Why the usual design fails

The usual design gives an agent two tools, `remember` and `recall`, and
expects the agent to call them.

`remember` fails because the decision to record competes with the task.
The agent is working on the problem, and the moment it learns something
durable is not a moment it spends thinking about memory.

`recall` fails because an agent does not know what it does not know. An
agent that would benefit from a decision taken last week has no signal
that the decision exists.

Memory that depends on a decision is absent exactly when it is needed.
This design records without a decision and returns without a decision.
Both tools exist beside those paths, and neither tool is the mechanism.

## The shape

The service is a blackboard. Background work writes to it, and every
read is a lookup.

Deciding what memory matters is the expensive part. The decision needs a
model, and a model takes hundreds of milliseconds. A conversation cannot
wait for that, so the decision leaves the turn path.

A background process reads the record, derives memory, ranks it, and
writes the result to a hot set in Redis, one hot set per person and
project. A turn reads the hot set. The read is an in-memory lookup, and
it calls no model.

Deriving memory is two passes. A cheap classifier reads a window and
scores which kinds of memory are in it, and a larger model reads only
what scored highly. The first pass stores a probability rather than a
decision about it, so the threshold belongs to whoever reads the table
and moving it costs a query instead of reading every window again.

| Part | What it does | On the turn path |
|---|---|---|
| A shim | A few lines of glue between a harness hook and an SDK | Yes, for capture and injection |
| The client | Sweeps session files, backfills, asks for memory | Only the ask |
| The collector | The front door. Receives OTLP, routes, buffers, retries | No |
| The service | Stores the record, schedules work, reads the blackboard | Yes |
| The blackboard | The hot set and the briefing, in Redis | Read only |
| The worker | Classifies, derives, ranks, consolidates, writes the hot set | No |
| Postgres | The record, the readings, and the memories | Behind the blackboard |

The blackboard is derived state. Empty Redis and the next read falls
back to Postgres, and the worker warms the hot set again. Nothing
authoritative is in it, so losing it costs latency and never data.

## The flows

The system has two paths, and they never meet. The turn path reads. The
background path writes. A model appears on one of them.

### Two paths

```
     THE TURN PATH                        THE BACKGROUND PATH

     a turn starts                        a record lands, or a clock fires
          │                                            │
          ▼                                            ▼
     the client asks                              the worker runs
          │                                            │
          ▼                                            ▼
     the service reads a key                    the worker calls a model
          │                                            │
          ▼                                            ▼
     the blackboard                             the worker writes memories
     one lookup, no model                       and the hot set
          │                                            │
          ▼                                            ▼
     the client injects                         the blackboard
          │
          ▼
     the turn proceeds

     no model is called                    every model call is here
```

Everything else in the design follows from that split. A model costs
hundreds of milliseconds, so it cannot be on the left. The ranking that
needs a model therefore happens on the right, ahead of time, and the
left reads what the right prepared.

### The map

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  surfaces                                                           │
  │   pi      Claude Code    OpenWebUI    OpenCode       Claude.ai      │
  └────┬──────────┬──────────────┬───────────┬─────────────┬────────────┘
       │          │              │           │             │
       │   capture and injection │           │       retrieval only
       │          │              │           │             │
  ┌────▼──────────▼──────────────▼───────────▼─────────────▼────────────┐
  │  the client                        one per machine                  │
  │  sweeps session files, backfills, asks for memory                   │
  └───────┬──────────────────────────────────────────────┬──────────────┘
          │                                              │
          │  OTLP, the record           HTTP, ask for memory
          │                                              │
          │        ┌──────────────────────────┐          │
          └───────►│  the collector           │          │
     OTLP ────────►│  routes, buffers, retries│          │
     (a harness's  └────────────┬─────────────┘          │
      own signals)              │  OTLP                  │
  ┌─────────────────────────────▼────────────────────────▼──────────────┐
  │  the service                                                        │
  │  stores the record, reads the blackboard, records what was injected │
  └──────────┬──────────────────────────────────────────┬───────────────┘
             │                                          │
   ┌─────────▼────────┐                      ┌──────────▼──────────┐
   │  the record      │                      │  the blackboard     │
   │  Postgres        │                      │  Redis              │
   │  everything said │                      │  what matters now   │
   └─────────▲────────┘                      └──────────▲──────────┘
             │                                          │
   ┌─────────┴──────────────────────────────────────────┴───────────────┐
   │  the worker                docket tasks, models, consolidation      │
   └─────────────────────────────────────────────────────────────────────┘
```

Three things in the map are worth stating plainly.

The client is the only part that knows about harnesses, so the service
and the worker are the same for every surface.

The worker is the only part that calls a model.

The record and the blackboard hold different things. The record holds
everything that was said, and it only grows. The blackboard holds what
matters now, and it is rewritten constantly.

### Capture

A turn happens, and the harness tells its shim. The shim emits a record.
Nothing waits, and nothing reads a file.

```
  a turn happens
      │
      ├──► the harness's hook ──► a shim ──► OTLP ──┐
      │                                            │
      └──► the harness's own telemetry ──► OTLP ───┤
                                                   ├──► collector ──► service ──► the record
  on a timer, and at session start:                │
      │                                            │
      ▼                                            │
  the client reads the session files ──► OTLP ─────┘
      (a repeat is dropped on its entry id)

  then, in the background, off the turn path:

  a new record ──► worker ──► memories ──► the blackboard
```

The record can arrive twice, once from a shim and once from the client.
The service drops the second copy on the entry id. The cost is bandwidth
on the wire, and the benefit is that the record never depends on a shim
behaving.

### Recall

A turn starts. The client asks, the service reads one key, and the
client injects.

```
  turn starts
      │
      ▼
  the client asks ──────────────────────────────► the service
      │                                               │
      │                                               ▼
      │                                        the blackboard
      │                                        one lookup, no model
      │                                               │
      │                                               ▼
      │                                        a cheap match of the
      │                                        prompt against the set
      │                                               │
      │◄──────────────────────────────────────────────┘
      ▼
  the client injects into the newest turn
      │
      ▼
  the service records which memories, and when
```

The hot set holds more than the turn needs. A session's first prompt is
handed the top of it. Every prompt after that is handed only what is
about the prompt: the service embeds the prompt with a small local model,
reads the nearest statements the session has not seen, and hands over the
few that score well above the rest, or nothing. The embedding takes
milliseconds and runs in the process, so the deadline below holds. The injected text goes into the newest user message, so the cached
prefix of the conversation survives. `plans/completed/04-the-match.md` holds
the match.

### Maintenance

A clock fires, or enough new records land. The worker changes the
memories and the hot set together.

```
  a clock fires, or enough new records land
      │
      ▼
  the worker reads the memories and the outcomes
      │
      ├──► merges statements that say the same thing
      ├──► ends a statement that a newer one replaces
      ├──► decays what nothing has used
      └──► re-ranks by trust and by what worked
      │
      ▼
  the blackboard, and a record of what changed
```

Nothing is deleted. A statement that stops being true takes an end date
and a replacement, and it leaves the hot set.

### The outcome

A turn ends, and telemetry says what it cost and whether it worked. The
worker joins that to the memories the turn received and adjusts them.

```
  a turn ends
      │
      ▼
  telemetry: cost, tokens, whether the work succeeded
      │
      ▼
  the service joins it to the record of what was injected
      │
      ▼
  the worker adjusts the utility of each memory
      │
      ▼
  the blackboard, so the next turn ranks better
```

This is the only flow that can tell a useful memory from a plausible
one. Without it the ranking is a guess that never improves.

## The record

The record is the raw material. It is append-only, and everything else
derives from it.

Every record carries its provenance: the machine, the harness, the
session, the parent session, the actor, the model, and the working
directory. The actor is a person, an agent, or the harness itself, and
it carries a depth. A prompt the person typed is depth zero. A prompt an
orchestrator wrote for a subagent is depth one.

That distinction is the one the extraction pass depends on. A subagent's
prompt arrives in the harness's log as a user message. A record that
marks an orchestrator's instruction as the person's own words teaches a
preference the person never stated.

The chain also records where it starts. A session a person began has a
person at its root. A session an autonomous agent began has no person
anywhere in it, because the agent chose to work.

Those two are the same actor at the same depth and they mean opposite
things. A subagent relays a person's intent from one level up. An
autonomous agent's statement is its own conclusion with nobody behind
it. Trust has to tell them apart.

### One wire, two jobs

Everything arrives as OTLP, through the collector. The wire carries two
different things, and they come from different places.

**The record comes from our shim, on every harness.** A harness's hook
hands the shim its content at the moment the content happens, in the
harness's own structure. The shim builds a record and emits it. Nothing
reads a file, and no harness format is parsed.

A harness that exports OpenTelemetry could send the conversation itself.
We do not use that, because the record has to be one shape everywhere.
Claude Code's export redacts content behind five separate flags, leaves
thinking blocks out, truncates at 60 KB, and names its fields in its own
schema, which it changes between releases. A shim gives the same record
from every harness and leaves the record in our hands.

**The numbers come from the harness, where it has them.** Cost, token
counts, and the spans that connect a prompt to the calls it caused are
not visible from a hook. Claude Code publishes them over OTLP, and a pi
extension produces the same signals from pi's own events.

**The client is the fallback.** It reads the session files a harness
wrote and ships what the other paths missed. It is also how history gets
in.

| | A shim | The harness's telemetry | The client |
|---|---|---|---|
| Produces | The record: what was said | The numbers: cost, tokens, spans | The record, as a fallback |
| When | As it happens | As it happens | On a timer, and at session start |
| Parses a harness format | No | No | Yes |
| One shape everywhere | Yes | No, and it need not be | Yes |

The service separates the two by instrumentation scope, and it joins
them by session and by turn.

A record from a shim and the same record from the client must carry the
same entry id, or the service stores both. The id comes from the harness
in either case, so the two agree, and that agreement is part of the
contract each adapter and each shim keeps.

The wire is one protocol, so no producer needs a transport of its own.
The collector owns the batching, the retry, and the buffering.

### What each harness exports

| Harness | Telemetry | How |
|---|---|---|
| Claude Code | Metrics, events, and traces | Native, behind `CLAUDE_CODE_ENABLE_TELEMETRY`, with traces behind a beta flag |
| pi | None natively | An extension, using the hook surface pi already exposes |

pi reports no OpenTelemetry of its own, and it needs no new capability
to do it. An extension can see every provider request and response, the
usage and the cost on each message, every tool execution with its start
and its end, and every compaction with its outcome. That is enough for
the same logs, metrics, and traces that Claude Code exports directly.

So the harness side of the wire is native where a harness ships it and
an extension where it does not.

### What the harness signals are for

They make a session legible as a whole. The spans connect a prompt to
the model calls and the tool calls it caused, with the tokens and the
cost on each.

They are also the only place that can say whether engaging a memory
made a turn cheaper or more likely to finish. The ranking learns from
that, and the baseline has to be captured before the memory exists.

### What the wire limits

The specification sets no limit on an attribute's length, and a log
record's body holds a string of any size. Two limits apply in practice.

- The collector accepts 4 MiB per request by default, and every record
  in a batch shares that budget. The client keeps a batch near 1 MiB.
- An SDK may cap an attribute's length, and 128 attributes per record is
  the common default. A record needs fewer than that.

So the 60 KB truncation Claude Code applies is Claude Code's own policy
and not a limit of the wire.

### The bodies stay in files

A body large enough to crowd a batch goes to a file, and the record
holds the path.

Claude Code already works this way. It writes the untruncated request
and response to files when `OTEL_LOG_RAW_API_BODIES` names a directory,
and the event carries the path. The service reads those files, so the
record holds the full body and the telemetry holds the reference.

A tool result too large for a batch goes to a file the same way, and the
record holds the path.

## The scope

A session belongs to a scope, and the scope is what a memory is filed
under. A memory about an organization is available in any repository
inside it, and a memory about one repository does not leak into a sibling.

The scope is derived from the directories the session is in. Nothing is
declared, because a declared layout goes stale the moment a directory
moves, and the filesystem already answers every question a layout would
have been written to answer.

Three questions are asked of each directory, from the working directory up
to the home directory.

| Question | Answer |
|---|---|
| Is it a git repository? | `repo` |
| Is it under a forge, and does it hold repositories? | `org` |
| Is its name a domain? | `forge` |

The directories that answer yes become the scope, joined by a slash. The
home directory is the boundary and is never a level. A directory that
answers none of the three leaves the scope to the top directory under
home.

```
~/src/github.com/acme/widget          github.com/acme/widget          repo
~/src/github.com/acme                 github.com/acme                 org
~/src/code.example.com/widget         code.example.com/widget         repo
~/src/git.example.com/team/gadget     git.example.com/team/gadget     repo
~/scratch                             scratch                         directory
~/.config                             .config                         directory
```

Three rows in that table are why the questions are these questions.

**`code.example.com` has no organization level.** A rule that counted
path segments would call `widget` an organization. This rule counts
repositories, so a forge with one level and a forge with two both work.

**`acme` is not a repository.** A rule that assumed the leaf is a
repository would find nothing there, and a person who works in that
directory most of the time would have no scope for the work. Holding
repositories is what makes it an organization.

**`~/scratch` holds repositories and is not an organization.** Stray
clones accumulate in it. Holding repositories is not enough on its own,
which is why an organization is a collection that belongs to a forge.

The key is a path, so retrieval inherits without a rule of its own. A
memory scoped to `github.com/acme` is available in
`github.com/acme/widget`, and a memory scoped to
`github.com/other-org/gadget` is not.

### What the scope is not

The scope is where a session started, not what it worked on. A session
that starts in an organization root scopes to the ecosystem even when it
touched one repository. The paths inside the tool calls say what the work
was, and a later pass can narrow a scope with them.

The client derives the scope, because only the machine knows its own
layout. The service stores what it is told.

## The memories

A memory is one atomic statement, in one sentence or two. It carries
seven things.

- **What it says.** One statement, in the person's own vocabulary.
- **What it is about.** The project it belongs to, or nothing for a
  statement that applies everywhere.
- **When it was true.** A valid interval, which may be open at either
  end.
- **When the service learned it.** A recorded time, which is a second
  clock and answers a different question.
- **Who said it.** The person, the actor, the depth, and the model.
- **How much to trust it.** A number bounded by the source.
- **What it came from.** The records that produced it.

A memory also carries how useful it has been: how often it was returned,
how often the turn that received it worked, and when it was last used.
The outcome flow writes those numbers, and the ranking reads them.

### The exchange is what is read

A reply means what it means next to the turn it answers. "No, the other
one" says nothing on its own, and a person's most useful statements are
the ones reacting to something. So what the service reads is the
exchange: a prompt and the agent turns between it and the prompt before
it.

What is stored is one statement, and the statement carries the records
the exchange came from.

A correction is the clearest case. It carries the most information of
anything in the record and needs its context the most, so a design that
read only the person's words would drop it.

### Six kinds

A memory is one of six kinds. The kinds are asked for directly rather
than inferred from the wording of a statement.

| Kind | What it holds |
|---|---|
| Semantic | Something about the code, the project, or how something works |
| Procedural | How something is done here: a command, a step, a way of working |
| Prospective | Something meant for later, or left unfinished |
| Preference | How one of the speakers wants things done, or something they dislike |
| Correction | One speaker pushing back on what the other did |
| Praise | One speaker approving of the other's work or approach |

Three kinds from the psychology of memory are absent. Episodic memory is
the record itself, so there is nothing to extract. Sensory memory has no
channel in text, and the part that maps onto a codebase is the layout of
the files, which is the scope. Working memory is the session in
progress.

The record is unusually good at procedural memory. What a person says
about how work is done is a small fraction of how the work was done, and
the record holds every command that was run.

The questions name the speakers rather than the person, because both
sides state facts, intentions, and preferences and the record holds both.
Which speaker said it belongs to the memory, and trust ranks a person's
statement above an agent's.

Correction and praise are the two kinds about the agent's own behavior.
They say what to stop doing and what to keep doing, and praise also feeds
the outcome counters, which record how often a turn that received a
memory worked.

### Time is on every memory twice

The valid interval is the period the statement was true in the world.
The recorded time is the moment the service learned it.

- "What did I believe on the first of March?" reads the recorded time.
- "What was true on the first of March?" reads the valid interval.
- "What did I believe on the first of March about what was true in
  February?" reads both.

Nothing is deleted. A statement that a later statement replaces takes an
end date and a link to its replacement, so a question about the past
still has an answer.

### Trust follows the source

A person's own words rank highest. An agent's statement ranks below
them, and a subagent's statement ranks below its parent's, because
distance from the person is distance from the intent. A statement from a
small model ranks below the same statement from a large one.

Trust does not filter. A low-trust statement is still returned, ranked
below a high-trust statement and labelled with its source, so a person
reading an injection can see that a subagent said it.

### Nothing is rewritten

Every change to a memory is an entry in a log, applied in order. No
update rewrites a statement in place. This gives the audit trail, the
rollback, and the answer to why the service believes something.

## Recall and injection

The turn path is one lookup and one score.

The service reads the hot set for the project from Redis, narrows it to
the prompt with a cheap local match, and returns what remains. The
client puts that text into the newest turn.

For pi the injection point is `before_agent_start`. For Claude Code it
is the `UserPromptSubmit` hook, which returns `additionalContext`. For
OpenWebUI it is a Filter with an `inlet`. The text becomes a new message
at the point of injection, so the cached prefix of the conversation
survives and only the injected text is new.

Claude Code blocks the turn until that hook returns, and its default
timeout is 30 seconds. A hook that stalls stalls the session, which is
why the deadline is ours, and why it is 150 ms.

Three rules hold the path to its invariants.

- A hard deadline of 150 ms. Past it the client injects nothing and the
  turn proceeds.
- The injection is visible. The terminal shows what arrived, and the
  session record carries it.
- A failure never reaches the model. A missing service, an empty hot
  set, and a dead Redis all produce the same result: no memory this
  turn.

An agent that wants to dig calls `recall` through a tool. The tool takes
a query and returns memories with their sources, and a second tool
returns the original conversation. The pull path exists for the agent
that knows what it is looking for.

## Where it runs

A surface reaches memory in one of three ways, and the way decides what
it gets.

**An interactive harness gets a shim.** The harness offers a hook at a
known moment, and a few lines of glue call the service there. The
harness owns the loop, so the shim works around it.

**An autonomous agent calls the service itself.** An agent that owns its
own loop needs no shim, because there is no loop to work around. It
calls the same API the client calls.

**A tool-only client gets retrieval on demand.** The model decides when
to call `recall`. The service cannot reach the prompt, so nothing is
ambient.

| Surface | How it reaches memory | Capture | Injection |
|---|---|---|---|
| pi | a shim on `before_agent_start` and `session_shutdown` | Yes | Yes |
| Claude Code | a shim on `UserPromptSubmit` and `SessionEnd` | Yes | Yes |
| OpenWebUI | a Filter with `inlet` and `outlet` | Yes | Yes |
| OpenCode | a plugin on `chat.message` and `event` | Yes | Yes |
| An autonomous agent | the agent's own loop calls the API | Yes | Yes |
| Codex | no hook exists | From the session files | No |
| Claude desktop and web | MCP tools and resources | No | Tools only |
| ChatGPT desktop and web | MCP tools and resources | No | Tools only |

The order of work follows the order of use: pi, Claude Code, OpenWebUI,
then Claude. The service does not change when a surface is added,
because the client is the only component that knows about harnesses.

### Latency binds an interactive surface only

A person waits for an interactive turn, so that turn path carries a
deadline of 150 ms.

Nothing else in the table waits for a person. An autonomous agent has
no deadline, so it can ask for more and wait while a model ranks what it
gets. One service answers both, and only the interactive surface takes
the cheap path.

### An autonomous agent has no prompt to hook

An interactive surface hands the service a moment, because the person
submits a prompt. An autonomous agent has no such moment, because it
never stops working.

Two things serve it instead.

- **Its own schedule.** A refresh is one more task in whatever queue the
  agent already runs.
- **A subscription.** The blackboard changes when the worker rewrites
  the hot set. An agent that subscribes learns that memory changed
  without asking.

### An autonomous agent is also a source

An agent that works on its own also says things worth keeping. Its
session ships like any other, and its statements enter the record with a
provenance that has no person in it.

### An SDK carries the protocol

The protocol is not the interesting part of an integration, and nobody
should write it twice. Each language that harnesses are written in gets
an SDK: the batching, the offsets, the retries, the deadline, and the
trace context.

A shim becomes a few lines of glue to a harness's hook. The SDK is also
the way to tie in a harness this project has never seen, because a
person writes the glue for their own harness against the same SDK the
shipped shims use.

### The MCP surfaces

The MCP specification offers one ambient slot for a surface with no
hook. A server returns free-form `instructions` in its `initialize`
answer, and a client may fold that text into the system prompt. Support
is not dependable, and Claude ignores the field.

A surface that offers only MCP gets retrieval tools and resources, and
nothing ambient.

## The client

Two pieces ship to a machine: a Go binary and an SDK for each language a
harness is written in.

The binary does everything off the turn path. It discovers session
files, reads them, ships records, and writes each harness's hook
configuration.

The sweep reads this machine. The same code reads a source elsewhere
when a person names it, and it records the machine that produced the
transcripts rather than the machine that ran the command.

An SDK does everything on the turn path, and it carries the protocol:
the batching, the offsets, the retries, the deadline, and the trace
context. A shim is then a few lines of glue to a harness's hook.

| Language | For |
|---|---|
| TypeScript | pi and OpenCode, which run their extensions in Node |
| Python | OpenWebUI, and any agent written in Python |

The turn path is in-process on three of the four interactive surfaces,
because the harness already has a runtime. A Claude Code hook starts a
process instead, and that process is the binary, which carries the same
record shape as an SDK.

Neither piece holds authoritative state. The binary remembers the
offsets it has read to, and the hot set is in Redis.

Go rather than Rust for the binary, because the difference between them
is about one millisecond of startup and the binary is not where the time
goes.

## Who it is for

One person per store. The store is the privacy boundary, and a second
person gets a second store.

A store is one service, one Postgres database, and one Redis prefix.
Stores share the host and the collector, and they share nothing else.

The boundary is the store rather than a row on a shared table, and the
reason is the shape of the failure. A query that reads the wrong person
puts one person's private conversation into another person's agent, and
nothing reports it. A separate database cannot fail that way.

This removes most of the authentication work. Every request carries one
token for its store, there are no user accounts, and the only tenancy
logic in the system is a map from an OpenWebUI user to a store.

A harness can serve more than one store. A shared OpenWebUI channel
serves one store for each person in it, or a third store the household
owns.

## What the design accepts

- **The service is a dependency.** With the service down, a turn has no
  memory. The turn continues, and the record catches up later.
- **Extraction costs money on every record.** The volume measured on one
  machine is about 92 MB per day. Extraction runs on a sample of the
  record until the cost per megabyte is known.
- **A wrong statement is retrievable.** Trust ranks a statement and does
  not remove it. The person and the maintenance pass are what correct
  it.
- **Provenance is only as good as the harness.** A harness that does not
  report its model leaves that field empty.
- **Re-deriving is not free.** A replay of a year of record costs a year
  of extraction.
- **The telemetry path is one harness today,** and the traces it exports
  are in beta.
- **The record holds secrets.** A session contains whatever the person
  pasted. Redaction is not attempted at ingest, because redaction loses
  the record.
- **A cold blackboard costs a round trip.** After a restart the first
  read falls back to Postgres, which is slower and still correct.

## What exists elsewhere

Several projects store memory for agents. None of them combines the
parts this design needs.

- **mem0**, **Zep**, and **cognee** are memory layers over a vector store
  or a graph database. They are libraries an application calls, and none
  of them records a coding session.
- **Letta** is a stateful agent runtime. Its memory belongs to an agent
  that runs inside Letta, and this design's agents run in harnesses the
  service does not control.
- **basic-memory** stores notes as Markdown behind an MCP interface. It
  has no ingest path and no time model.
- **Claude-Mem** captures sessions through Claude Code hooks and
  compresses them into observations. Its store is a database, its scope
  is one project, and its default install path uses a hosted provider.
- **engram** is the closest in shape. It keeps a canonical extraction
  model on the server, preserves raw input, re-derives from it, and
  records provenance. It reaches memory only through calls an agent
  decides to make, and its author stopped working on it in June 2026.
- **SpecStory** and **deja** read the session files the harnesses already
  write. Both stop at capture. This design takes that ingest path and
  continues past it.

## The plans

`plans/README.md` indexes the plans that build this design. Each plan
states a problem and how the work is proved, and it leaves the shape of
the code to whoever builds it.
