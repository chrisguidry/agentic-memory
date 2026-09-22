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
    agentic-memory version    print the version

`bastion` answers two routes itself and proxies the rest to the service with
the authorization header added.

- `POST /claude-code/hooks` takes Claude Code's hook payload. On
  `UserPromptSubmit` it derives the scope from `cwd`, asks the service for
  memory inside the deadline, and answers with the `hookSpecificOutput`
  envelope that carries the block. On `Stop`, `SubagentStop`, `SessionEnd`,
  and a turn with no memory, it answers with nothing. After the answer is
  written, it reads what the transcript gained and ships the lines to
  `POST /v1/transcripts`.
- `POST /transcripts/ship` takes `{"harness", "path", "machine", "cwd"}` and
  answers with `received`, `inserted`, and `repeated` summed over every batch
  the file took, so a caller sees the whole file.

`top` polls the socket for `GET /memories` and `GET /classifications`, and draws
three panels: what is worth remembering for this directory's scope, what the
writer has just produced, and what the classifier has just read. `--scope` names
another scope and `--all-scopes` reads every one. `--limit`, `--new`, and
`--read` are upper bounds on the three panels, and the frame gives way from the
top down until it fits the terminal. `--every` is the seconds between polls.

`claude` reads the payload on stdin, sends it to the socket, writes the answer
to stdout, and exits zero whatever happened. It has a hard deadline of two
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
record will say the wrong machine.

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
ends agree without either holding the other's configuration. Offsets are kept
under `$XDG_STATE_HOME/agentic-memory/claude-code/`.

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
