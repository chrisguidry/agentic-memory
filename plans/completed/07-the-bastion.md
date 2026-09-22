# 07, The bastion

Closed 2026-09-22. Built in the commit that built it, and closed after.

## The problem

Every turn in Claude Code runs two Python processes. One ships what the
transcript gained, and the other asks the service for memory. Each costs
50 to 85 milliseconds to start before it opens a socket, and the recall
hook then pays a TCP and TLS handshake to a service across the network,
because nothing on the machine holds a connection open between turns.
The service's own work is the part that cannot be compressed, so every
other millisecond on the turn is ours to remove.

The credentials have the same shape of problem. Each client reads the
service's address and authorization from its environment, so the secret
has to be present in every shell that might start a harness, and a shell
without it silently gets no memory. Today's recall log shows exactly
that: turns refused on the loopback default because the variables were
not set.

The client side is also four Python programs and a TypeScript extension
that each know the service's address, and three of them read the
transcript formats. `tools/harnesses/` is meant to be the only place a
format is read, and the live hook reads it too.

## The shape

One long-lived process per user, the bastion, and one Go binary that
holds it and every other client.

```
  agentic-memory claude ──┐   POST /claude-code/hooks
  the pi extension  ──────┤   POST /v1/logs, POST /recall
  agentic-memory top ─────┤   GET  /memories, GET /classifications
  agentic-memory backfill ┤   POST /transcripts/ship
                          │
                          ▼
   $XDG_RUNTIME_DIR/agentic-memory.sock   (mode 0600, owned by the user)
                          │
                          ▼
              agentic-memory bastion
                • holds the service's address and authorization
                • one warm HTTPS connection to the service
                • proxies every path it does not handle itself
                • derives the scope from the filesystem
                • tails transcripts and ships their raw lines
                          │
                          ▼
                    the service (HTTPS)
                      POST /v1/transcripts   parses the lines with the
                                             harness readers, which now
                                             live in the server
```

The socket speaks plain HTTP. Anything already written against the
service works against the socket unchanged, and the bastion adds the
authorization header on the way through. The kernel enforces who may
connect, so no token travels in any client's environment, and nothing
listens on the network.

The hook knows nothing. `agentic-memory claude` reads the payload Claude
Code gives it on stdin, POSTs it to the socket, writes the response body
to stdout, and exits zero whatever happened. It has a hard deadline of
its own, so a wedged bastion cannot hold a turn to Claude Code's hook
timeout. `agentic-memory codex` and every later harness's hook is the
same program with a different path.

The bastion does the work the service cannot do from a `cwd` string. On
`UserPromptSubmit` it derives the scope, asks the service for recall
within the deadline, renders the block, and writes the response. Only
after the response is on the wire does it read what the transcript
gained and ship it. On `Stop`, `SubagentStop`, and `SessionEnd` it
responds at once and ships after.

The bastion ships bytes, not records. It remembers an offset and a
fingerprint per transcript, the way the Python hook does today, and
sends the new lines whole. The service parses them with the reader it
already has, and the harness readers move from `tools/` into `server/`.
A format is read in one place, on the server, and the bastion never
learns one.

Shipping is retried. The bastion is long-lived, so a file whose send
failed stays in its memory and is retried with backoff until the service
accepts it. The offset advances only on a 2xx. A file that is behind is
a line in the journal.

## The contracts

### The socket

`$XDG_RUNTIME_DIR/agentic-memory.sock`, created by a systemd user socket
unit with mode 0600 and handed to the bastion as fd 3. Run by hand, the
bastion creates it itself. `AGENTIC_MEMORY_SOCKET` overrides the path
for every client and for the bastion.

### The bastion's environment

- `AGENTIC_MEMORY_SERVICE`: the service's base URL.
- `AGENTIC_MEMORY_AUTHORIZATION`: sent whole as the `Authorization`
  header on every upstream request.
- `AGENTIC_MEMORY_RECALL_DEADLINE_MS`: default 500.
- `AGENTIC_MEMORY_RECALL_LIMIT`: default 10.
- `AGENTIC_MEMORY_MACHINE`: default the hostname.

The systemd unit reads these from `~/.config/agentic-memory/environment`
(mode 0600). Nothing else on the machine holds them.

### The bastion's routes

- `POST /claude-code/hooks`. The body is the Claude Code hook payload,
  verbatim. The response body is what the hook prints to stdout: the
  recall block as plain text on `UserPromptSubmit`, and nothing on every
  other event. The status is always 200.
- `POST /transcripts/ship`. `{"harness": ..., "path": ..., "machine":
  ..., "cwd": ...}`. Ships what that file gained since the bastion last
  saw it, and answers with what the service counted. Backfill calls this
  once per file.
- Everything else is proxied to the service with the authorization
  header added, over one kept-alive connection.

### The service's new route

`POST /v1/transcripts`:

```json
{
  "harness": "claude-code",
  "machine": "laptop",
  "path": "/home/someone/.claude/projects/-home-someone-src-widget/abc.jsonl",
  "cwd": "/home/someone/src/github.com/acme/widget",
  "version": "2.1.278",
  "scope": "github.com/acme/widget",
  "scope_kind": "repo",
  "repository": {
    "name": "widget",
    "owner": "acme",
    "url": "git@github.com:acme/widget.git",
    "branch": "main",
    "revision": "0123abcd"
  },
  "entries_before": 12,
  "lines": ["{...}", "{...}"]
}
```

`lines` are the file's whole lines from the offset, undecoded beyond
UTF-8. `entries_before` is how many entries preceded them, so the reader
numbers the entries the way a read of the whole file would. `cwd` and
`version` are the first the bastion ever saw for this file, because a
chunk from the middle of a file may carry neither. `scope` and
`scope_kind` are derived by the bastion, and the reader uses them
instead of deriving its own. `repository` is what `git` says about `cwd`,
read by the bastion once per directory, and every field in it is
optional; the reader carries them as the `vcs.*` attributes the Python
client attached, so the record's provenance is what it was. The reader turns the lines into the same
records, with the same entry ids, that a backfill of the whole file
produces, and the service stores them through the same path as
`/v1/logs`. The answer is the same counts `/v1/logs` gives. A 2xx is the
confirmation the bastion advances its offset on.

### The hook

`agentic-memory claude` exits zero always, prints nothing on any
failure, and finishes within two seconds whatever the bastion does. It
never touches the network or the filesystem beyond the socket.

### The block

The recall block is rendered exactly as `tools/recall.py` renders it
today: the heading, then one line per statement with kind, scope, who,
age, and the statement. The Go rendering is checked against the Python
one on the same statements.

## What proves it

- A cold start of `agentic-memory claude` finishes under 5 milliseconds
  on the build machine, measured by a test that fails when it does not.
  Every subcommand shares the binary, so a dependency that adds package
  initialization fails this test.
- The response on `Stop` is written before the transcript is opened,
  proved by a test whose transcript read blocks until the response is
  read.
- A transcript sent in chunks through `/v1/transcripts` stores the same
  entry ids as the same file backfilled whole. That is the test that
  guards the offset design, and it uses invented transcript content.
- With the service refusing connections, a session's events are
  accepted by the bastion, retried, and stored once the service returns,
  with the offset advanced exactly once.
- The drill: the local Compose stack, a Claude Code session with the
  hook registered, and `agentic-memory top` showing the session's
  statements land. The measurement is the hook's wall time on a
  `UserPromptSubmit` against the recall path.

## What the drill measured

A `UserPromptSubmit` that recalled ten statements through the bastion to
the real service took 30 ms of hook wall time, counted from the hook's
first instruction to its exit. The two Python hooks it replaced spent 50
to 85 ms on interpreter startup alone, before either opened a socket.

A cold start of `agentic-memory claude` is 3.4 ms, against the 5 ms the
test fails at. A warm request proxied over the socket round-trips in
about 34 ms, and most of that is the service answering.

## What stays open

- pi still builds its own records in TypeScript, so its live path is
  the one place a format is read outside the server. The extension
  moves to the socket in this plan and keeps its records.
- The first prompt after the bastion restarts pays the TLS handshake.
- A session whose working directory changes mid-file keeps the scope
  the bastion derived from the first one it saw.
