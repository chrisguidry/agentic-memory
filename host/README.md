# The host binary

`agentic-memory` is what runs on a person's own machine. One process, the
bastion, holds the service's address and authorization and one warm
connection to it. Every other client speaks plain HTTP to a unix socket the
bastion serves, so no token travels in any shell's environment and nothing
listens on the network.

## The subcommands

    agentic-memory bastion    serve the socket
    agentic-memory claude     the Claude Code hook
    agentic-memory backfill   load the sessions a harness already wrote
    agentic-memory top        watch what the memory loop is producing
    agentic-memory label      judge a sample of prompts and the statements
                              they were handed
    agentic-memory version    print the version

`bastion` answers five routes itself and proxies the rest to the service with
the authorization header added.

- `POST /claude-code/hooks` takes Claude Code's hook payload. On
  `UserPromptSubmit` it derives the scope from `cwd`, asks the service for
  memory inside the deadline, and answers with the `hookSpecificOutput`
  envelope that carries the block. On `Stop`, `SubagentStop`, `SessionEnd`,
  and a turn with no memory, it answers with nothing. After the answer is
  written, it reads what the transcript gained and ships the lines to
  `POST /v1/transcripts`.

  The bastion counts every recall that misses the deadline. The count goes
  to the service as `missed` in the body of the next `POST /recall`, and the
  service adds it to `agentic_memory_recall_deadline_misses_total` on its
  `/metrics`. Prometheus does not scrape a person's machine, so this is how
  a miss reaches the dashboard. The count is taken as delivered once the
  request is written. A request that fails before it is written keeps its
  count for the next recall.
- `POST /claude-code/probes` takes the same payload and gives the same answer
  for a probe session. It stores nothing. [Probing recall](#probing-recall)
  has the details.
- `GET /claude-code/probes` answers `{"probes": true}`, so a validator can
  confirm the bastion takes probes. With `?transcript=<path>` it also names
  what the bastion records for that transcript: `probe`, `live`, or `none`.
- `POST /transcripts/ship` takes `{"harness", "path", "machine", "cwd"}` and
  answers with `received`, `inserted`, and `repeated` summed over every batch
  the file took, so a caller sees the whole file.
- `GET /transcripts/behind` answers `{"behind": 2}`: how many files the
  bastion has not shipped yet.

`top` polls the socket for `GET /memories`, `GET /classifications`, and
`GET /transcripts/behind`, and draws three panels: what is worth remembering
for this directory's scope, what the writer has just produced, and what the
classifier has just read. The status line under them says how many
transcripts are behind, when any are. `--scope` names
another scope and `--all-scopes` reads every one. `--limit`, `--new`, and
`--read` are upper bounds on the three panels, and the frame gives way from the
top down until it fits the terminal. `--every` is the seconds between polls.

`label` walks a sample built on the service with `agentic-memory-sample` and
takes a person's judgment of each pair through `GET /labels/next` and
`POST /labels`, both proxied by the bastion like any other route. It shows
where and when the prompt was said, the agent's last reply before it, the
prompt, and the statement with its kind, its scope, and how long ago it was
said, one pair to a screen. `--sample` names the sample to walk:

    agentic-memory label --sample week-1

`g`, `n`, and `w` judge the pair good, noise, or wrong and move to the next
one. `s` skips it without judging, and comes back to it once every later pair
in the sample has been judged. `c` opens the session's earlier exchanges
through `GET /labels/context`, one at a time and newest first; `c` again
reaches further back, and any other key returns to the pair. `q` quits at any
time, from the pair or from context; a judgment already sent stands, because
a label outlives the sample that asked for it.

`claude` reads the payload on stdin, sends it to the socket, writes the answer
to stdout, and exits zero whatever the bastion did. It exits 1 only for an
`AGENTIC_MEMORY_PROBE` value it refuses. It has a hard deadline of two
seconds, so a wedged bastion cannot hold a turn. Register it in
`~/.claude/settings.json` under `UserPromptSubmit`, `Stop`, `SubagentStop`,
and `SessionEnd`:

    {"type": "command", "command": "/home/someone/bin/agentic-memory claude",
     "timeout": 5}

`backfill` finds a harness's session files and asks the bastion to ship each
one. It takes `--harness` (`claude-code`, `codex`, or `pi`), `--from` for a
source directory other than the harness's own, `--machine` for transcripts
that came from another machine, and `--dry-run` to list the files and send
nothing:

    agentic-memory backfill --harness claude-code
    agentic-memory backfill --harness codex --machine desktop --from /mnt/old/sessions

Name the machine when the transcripts came from somewhere else, or every
record will say the wrong machine. A backfill ships nothing of a probe
session, which [Probing recall](#probing-recall) describes.

## Probing recall

A probe session is a Claude Code session that gets the memory a live session
would get, and leaves no trace in the store. Use one to judge recall from real
prompts in a real repository without the test becoming part of the person's
history. Run it in a clone outside the person's own trees, and set
`AGENTIC_MEMORY_PROBE=1` in the environment of the session:

    git clone ~/src/example.test/acme/widget ~/tmp/probes/widget
    cd ~/tmp/probes/widget
    AGENTIC_MEMORY_PROBE=1 claude -p "how do the tests reach the database?"

The hook inherits the variable from Claude Code. `1` and `true` mark a probe,
and the hook posts every event to `POST /claude-code/probes` in place of
`POST /claude-code/hooks`. Empty, `0`, and `false` mark a live session. The
hook refuses any other value: it forwards nothing, writes the reason to
stderr, and exits 1, and Claude Code shows a hook error and goes on without
memory.

For a probe event, the bastion does these things:

- Before it answers, it marks the transcript's offset record as a probe's.
  The shipper never ships a marked transcript, whoever asks: a later live
  event, a retry, a restarted bastion, or a backfill. The mark also covers the
  subagent transcripts Claude Code writes under `<session>/subagents/`. Nothing
  removes the mark.
- On `UserPromptSubmit`, it asks the service's `POST /recall/probe` and prints
  the same block a live turn prints.
- It never ships the transcript, on any event.
- It keeps the ids of the statements it handed each probe session in memory,
  and sends them as `"seen"` with the session's next probe. The next prompt
  then takes the match form, as it would in a live session. `SessionEnd`
  forgets the session, and so does a restart of the bastion.
- A probe that misses the deadline is not counted. The `missed` count that
  goes with the next live recall holds only live misses.

Never resume or continue a probe session. `claude --resume` writes a new
transcript that copies the probe's conversation, at a path the bastion has
never marked, and a live event ships it. Never start a probe from a person's
own session either, because a probe event marks the transcript it names, and
a marked transcript never ships again. A clone outside the person's own trees
keeps `claude --continue` in a real tree from picking up a probe session.

The service chooses a probe's statements on the same path as a live turn's:
the plumbing filter, the filter for short replies, the match, and the opening
list. It writes no injection row. It counts a probe under
`agentic_memory_probe_recalls_total`, by form and outcome, so the live
`agentic_memory_recalls_total` holds only what a person's sessions asked. The
latency histograms count probes and live recalls together. `POST /recall`
refuses a body with `probe`, `seen`, or any other field it does not name, and
`POST /recall/probe` refuses `missed`, with 422.

A bastion or a service that has no probe route answers 404 for it. Then the
probe gets no memory, and nothing is stored. That is safe, and it is also a
probe that measures nothing, so check both ends before the first session,
and the transcript after its first event:

1. Ask the bastion whether it takes probes. Anything but `{"probes": true}`
   is a bastion that has no probe route, and a probe through it gets no memory.

       curl -s --unix-socket "$XDG_RUNTIME_DIR/agentic-memory.sock" \
         http://bastion/claude-code/probes

2. Ask the service for a probe through the bastion, which proxies the path.
   A service that takes probes answers 200 with a list of statements, and
   records nothing. A 404 is a service that has no probe route.

       curl -s -w '%{http_code}\n' --unix-socket "$XDG_RUNTIME_DIR/agentic-memory.sock" \
         -H 'Content-Type: application/json' \
         -d '{"session_id": "preflight", "harness": "claude-code"}' \
         http://bastion/recall/probe

3. After the first event of the first probe session, ask for its transcript.
   The transcript is `~/.claude/projects/<cwd>/<session_id>.jsonl`, where
   `<cwd>` is the working directory with every `/` and `.` written as `-`, and
   `claude -p --output-format json` prints `session_id`. The answer must name
   the record `probe`. `live` means the transcript has an offset record with no
   probe mark, and `none` means the bastion never marked it. Stop the probes on
   either one.

       curl -s --unix-socket "$XDG_RUNTIME_DIR/agentic-memory.sock" \
         --get --data-urlencode "transcript=$transcript" \
         http://bastion/claude-code/probes

## The environment

The bastion reads these, and nothing else on the machine holds them:

| variable | default |
| --- | --- |
| `AGENTIC_MEMORY_SERVICE` | none, and the bastion refuses to start without it |
| `AGENTIC_MEMORY_AUTHORIZATION` | none, and no header is sent |
| `AGENTIC_MEMORY_RECALL_DEADLINE_MS` | 500 |
| `AGENTIC_MEMORY_RECALL_LIMIT` | 10 |
| `AGENTIC_MEMORY_MACHINE` | the hostname |
| `AGENTIC_MEMORY_SOCKET` | `$XDG_RUNTIME_DIR/agentic-memory.sock` |

`AGENTIC_MEMORY_SOCKET` is read by the bastion and by every client, so both
ends agree without either holding the other's configuration.

## The state

The bastion keeps one record per transcript under
`$XDG_STATE_HOME/agentic-memory/claude-code/`, which defaults to
`~/.local/state/agentic-memory/claude-code/`. The file name is a hash of the
transcript's path. Each record has mode 0600 and holds the offset the bastion
has shipped to, a fingerprint of the file's head, and the first `cwd` and
`version` the file carried. A probe session's transcript has a record with
`"probe": true` and no offset, and the bastion never ships it. The bastion writes a record to a temporary file
and renames it into place, so a record is never half written.

A record also holds a `behind` object while the service has not taken what
the file gained: the request to send again, how long to wait, and when. A
restarted bastion reads every record with a `behind` object and ships each
file from its saved offset. The bastion sends a file in batches of 200 lines
and saves the offset after each batch the service takes, so a file that fails
partway resumes after the last batch taken, and no line is sent twice.

## When a transcript does not ship

A failure to ship has one of three causes, and each has its own handling.

| cause | example | handling |
| --- | --- | --- |
| the file does not exist yet | a session's first prompt, before Claude Code writes the transcript | wait up to two minutes for it, then drop it |
| the file is gone | a transcript deleted after the bastion shipped from it, a directory, a file the person cannot read | one line in the journal, then drop it |
| the service or the network failed | a 503, a DNS failure, a TLS error | retry after 5 seconds, doubling to at most 5 minutes, with no limit on attempts |

A dropped file loses nothing the file still holds. Its offset stays in its
record, and the next event in that session ships from there.

## Running it

Under systemd, copy the units in `systemd/` to
`~/.config/systemd/user/`, write `~/.config/agentic-memory/environment` with
mode 0600, and enable the socket. systemd creates the socket and hands it to
the bastion as file descriptor 3, so the first client's connection starts the
bastion.

By hand, the bastion creates the socket itself:

    go build -o ~/bin/agentic-memory .
    AGENTIC_MEMORY_SERVICE=https://memory.example.test \
      AGENTIC_MEMORY_AUTHORIZATION='Basic ...' \
      ~/bin/agentic-memory bastion

It logs one line per event to stderr and stops cleanly on `SIGTERM`.
