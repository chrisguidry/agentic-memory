# Working on agentic-memory

`agentic-memory` keeps one person's memories across every coding agent
and every machine they use. A hook on each machine sends what the person
and their agents say to a service. The service stores it, derives memory
from it, and returns the memory that matters to an agent when the agent
starts work.

`plans/00-design.md` is the design. `plans/README.md` indexes the plans
that build it. Code exists only where a plan calls for it. A plan states
contracts and leaves the shape of the code to whoever builds it.

## Voice

Every word in this repository follows `~/.ai/skills/writing/voice.md`.
The rules cover the plans, the comments, the commit messages, the
README, and the CLI help text. Read the file before you write, and run
its tripwire scan before you publish.

## Privacy

This repository is public, and the system it describes is not. The
service holds one person's private conversations with their agents. Keep
real session content, real hostnames, real repository paths, and real
credentials out of every plan, comment, commit message, and test
fixture. A fixture uses invented content.

## The lab

The service needs Postgres, Redis, and an OpenTelemetry collector. A
plan that changes the record, the memory model, or the extraction
pipeline names the drill that proves it, and the drill runs against a
local stack. Nothing is deployed to the homelab before the drill passes.
