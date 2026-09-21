# The plans

This directory holds the design documents. Each document is numbered in
sequence, and its number never changes.

[`00-design.md`](00-design.md) is the design. It describes the shape of
the system and the flows of information through it. It does not describe
an implementation.

The numbered plans build the design, in order. A plan states a problem
and how the work is proved, and it leaves the shape of the code to
whoever builds it.

A plan is a phase of work that closes on its own. A flag on a command, a
field on a record, or a second way to call something the plan already
builds belongs to the plan that owns it, and does not become a plan of
its own. The numbers are build order, so a plan that is more specified
than its neighbours should also be earlier than them.

An artifact carries the level of detail its certainty supports. A plan
starts as a sketch, because a decision nobody has tested does not
deserve an interface. Detail arrives when someone is about to build the
thing, and not before.

A plan stays current until it closes. When the design moves, every open
plan the move touches moves with it, in the same commit. A stale plan
costs more than no plan, because it describes a system that no longer
exists.

A plan moves to [`completed/`](completed/) when it is built. A plan that
is set aside moves to [`rejected/`](rejected/) with the reasons that
decided it. A question the current work cannot answer is written to
[`open-problems/`](open-problems/). Those documents have no number
because no work item exists for them yet.

A plan closes in the commit that builds it. That commit moves the
document to `completed/`, dates its header, and states what the drill
measured.

## Planned

* [01, The record](01-the-record.md). The warehouse: live capture through
  a harness hook and a shim, an OTLP ingest path through the collector, a
  Go client that sweeps and backfills session files from this machine or
  from a named source, and the first SDK. No model is called and nothing
  is derived.
* [02, The classifier](02-the-classifier.md). The first of two passes that
turn the record into memory. A worker reads a window of a session, asks a
cheap model which kinds of memory are in it, and writes down the
probabilities. No memory is written.

Nothing after this is written. The design says where the work can go, and
a plan is written when the work is about to start. A plan written now for
a step six months out would be a guess with a number on it.

## Completed

Nothing is built.

## Open problems

* [Where the bodies live](open-problems/where-the-bodies-live.md). The
  record fits in Postgres at the measured volume, and the measurement
  covers one machine and two months. The point at which bodies move to
  object storage is a guess.
* [A session in two stores](open-problems/a-session-in-two-stores.md). A
  store is the privacy boundary, and a harness can serve more than one
  store. What a session reads and writes when it belongs to two is not
  decided, and the options are worth trying in the open.

## Rejected

Nothing is rejected.
