# 08, The replay

Closed 2026-09-27.

## The problem

Nothing measures whether the turn path helps. The injections table records
what each turn was handed, and nothing says whether a handed statement was
about the prompt. A change to the match, the opening list, or the questions
is judged today by reading a few turns by hand, and the next change is
judged on different turns.

Nothing measures how long the turn path takes, either. The api exposes
counts of rows and of model calls, and no latency. Over one week of use the
bastion logged 68 recalls that missed its 500 ms deadline, which is about
one in ten, and the service has no record of any of them. Which phase takes
the time, the embedding, the query, or the network between the laptop and
the cluster, is unknown.

One question follows from the misses. When the bastion stops waiting, the
service may still finish the recall and write an injection row. The session
is then recorded as having seen statements that never reached it, and they
are never offered to it again.

## The shape

Three parts, which share one purpose: every later plan is measured against
the same prompts and the same labels.

```
  the record's real prompts          the person
          │                               │
          ▼                               ▼
  replay: recall as of a moment     label: good, noise, or wrong
  under the rules in the code       for one prompt and one statement
          │                               │
          └──────────────┬────────────────┘
                         ▼
            the report: counts, and the labelled share
```

### The replay

The replay reads the real prompts a range of the record holds, and runs
each one through the turn path as the code now stands, against the
statements that were live at the moment the prompt was said. It writes no
injection row. It keeps, per session, what that session was handed earlier
in the replay, so the opening list and the unseen filter behave as they did
live.

The report gives, over the range:

- statements handed per turn, and the share of turns handed nothing
- the share of handed statements that have no scope
- the handed statements by kind
- the share of handed pairs that are labelled, and of those, the share
  labelled good, noise, and wrong

Two replays of one range under two versions of the code give a diff: the
pairs only the first handed, and the pairs only the second handed.

### The labels

A label judges one pair: one prompt and one statement it was handed. It
does not judge a turn. A pair outlives the rules that produced it, so when a
later plan changes what the turn path hands out, the replay reuses every
pair already labelled and asks only about the new ones.

A label is one of three:

- **good**: the statement is about the prompt, and an agent is better off
  for having it
- **noise**: the statement is true and harmless, and the prompt did not
  need it
- **wrong**: the statement is false, out of date, or belongs to another
  place

The first sample is about 150 pairs drawn from the injections of the last
week. It is stratified by form (the opening list and the match), by kind,
and by whether the statement has a scope.

`agentic-memory label` is a subcommand of the host binary, beside `top`,
and it is built from the same terminal pieces. It shows the prompt, the
statement, the statement's kind, scope, and age, and takes one key per
label, a key to skip, and a key to quit. It reaches the service through the
bastion's socket, the same way `top` does. The content is the person's own
sessions, so it goes nowhere the record does not already go.

### The metrics

The api records a histogram per phase of a recall: the embedding of the
prompt, each query, and the whole request. It counts recalls by form and by
outcome: statements handed, nothing handed, and an error.

The bastion is on the person's machine, where the cluster's Prometheus does
not scrape. When a recall misses the deadline, the bastion counts the miss
and sends the count with its next request to the service, which adds it to
a counter. A miss is then visible on the same dashboard as the latency that
caused it.

The dashboard in `deploy/monitoring/dashboards/` gains a row for the turn
path.

## The contracts

- A `labels` table holds one row per pair: the session and entry of the
  prompt, the statement's id, the label, and when it was given. A pair has
  one label, and a second label for the pair replaces the first.
- The service has routes for the next unlabelled pair of a sample and for
  writing a label. The bastion proxies both, as it proxies the rest.
- The replay is a command on the service's side, because it needs the
  embedder and the database. It names its range and the moment the store is
  read as of, and prints the report.
- The recall request from the bastion carries the count of misses since its
  last request.

## What it does not do

- **It does not tune anything.** Plans 10, 11, and 12 read the replay and
  the labels to set their cutoffs. This plan only builds the measure.
- **It does not feed labels into the ranking.** The outcome flow in the
  design does that, later.

## How it is proved

- A replay of the last week, against the store as of the end of the week,
  reproduces the counts of the live injections within a few turns. Turns
  that missed the deadline live are the expected difference.
- A pair labelled once is not offered again, in the TUI or by a later
  replay.
- The histograms and the counters appear on `/metrics`, and the dashboard
  row reads them.
- The injection question has an answer: a test that holds a recall past
  the client's deadline shows whether a row is written. If a row is
  written, the service stops writing it for a request whose client has
  gone, and the test proves that too.
- The suite runs against a real Postgres, and fixtures use invented
  content.

## Open questions

- **Whether 150 pairs are enough.** The first sample sets the order of
  magnitude for the next ones.

## What the drill measured

The replay ran against a local restore of the homelab store, over
2026-09-21 to 2026-09-28, as of 2026-09-27T14:00Z. From 2026-09-23, where the
live injections are comparable, it agrees with them:

| | replay | live |
|---|---|---|
| first turns | 26, of 10 statements each | 26, of 10 statements each |
| later turns handed something | 71, averaging 1.31 | 89, averaging 1.35 |
| handed statements with no scope | 42.8 percent, 151 of 353 | 40.0 percent, 152 of 380 |

Matched turn by turn, 298 of 311 later turns agree on whether anything was
handed. Where both handed something, 61 of 63 handed the same set, and 20
first turns handed the same 200 pairs. The 18 later turns only the live
path handed are recalls on task notifications and a skill's body, which the
replay excludes and the live path does not, and injections with no prompt
in the record. The rest of the difference is the bastion's deadline: six
first turns missed it live, so the live path handed the opening list one
prompt later.

The live path did write an injection row after the client had gone. A test
held a recall past a 0.3 second client deadline, and the row appeared. The
service now checks for a disconnected client just before the write, and
counts that recall under the outcome `gone`. A client that leaves during the
insert itself is still recorded.

Reading what a session was handed once, as an array, in place of a check per
row against `injections`, took the nearest query from 25.4 ms to 6.5 ms at
the median on the live data, and the opening query from 10.3 ms to 3.7 ms.

The labelling drill ran the real bastion and `agentic-memory label` against a
local service with invented data. A sample of four pairs split into one
opening and three matches, each key landed as a label, and a second label
for a pair replaced the first.

The replay reads each prompt against the statements live at its moment. A
re-read writes statements after the week it replays, so plans 11 and 12 add
a mode that reads the store as it is now.
