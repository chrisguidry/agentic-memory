# 03, The memories

Closed 2026-09-21. Built in the commit that closed it.

## The problem

The second pass writes one sentence for each kind of memory found in a
message that scored highly. It fills a table that nothing retires and
nothing ranks for a moment in time. Two things are missing.

**Nothing retires a statement.** A person changes their mind, and both
the old statement and the new one stay in the list. The design says a
statement that a newer one replaces takes an end date and a link to its
replacement. Until that exists, every statement is permanent, and a list
that only grows is a list that stops being read.

**Nothing ranks a statement for now.** The list is ordered by the
classifier's probability, which answers how likely it was that the
message held a memory of that kind. That question was answered when the
statement was written, and the ordering it produces follows the rate at
which each kind's question fires. Praise and prospective fire on most
messages and semantic fires on few, so the reading with the most
confidence in the corpus, a semantic statement at 0.87, ranks below
every praise statement but one.

The second pass that fills the table is built. This plan governs what a
row means once it is there, how a row leaves, and what order a scope
reads them in.

## The shape

A statement is written, it stands, and a later statement retires it.

```
  a message scored highly by the classifier
        │
        ▼
  the writer reads the message, writes a sentence   ──►  a memories row
        │                                                     │
        │  if the message pushes back on something            │
        ▼                                                     │
  the statements already held about this place  ─────────────►│
        │                                                     │
        ▼                                                     ▼
  the writer names the ones the new sentence replaces    superseded_by
        │                                                     │
        ▼                                                     ▼
  retire them                                          out of every read
```

Retiring is a pointer from the older statement to the newer one, with
the moment the service learned about it. The older statement is never
deleted and never edited, so a question about the past still has an
answer, and the chain of pointers is the reason the service believes
what it believes.

The moment of retirement is a recorded time and not the end of the
valid interval. The service learns that something stopped being true at
a different moment from when it stopped, and the design keeps those two
apart on purpose.

A statement's scope is decided by the writer, from the sentence it wrote.
The classifier asks whether the message applies beyond this project, and
one message can hold a general rule and a fact about this project at
once, so answering that question for every sentence it produced filed
sentences about this project under every project. Measured on one
machine's history, 15 of the 62 statements reachable from every project
named the project they came from. The writer marks each sentence now, and
the same measurement finds none of 24.

## The slice a statement is compared against

Retirement needs to find the statements a new one could replace. The
obvious shape is to compare every new statement against every memory,
which is the search this design avoids everywhere else.

The comparison is affordable without any special machinery, because the
set is small. Measured over one machine's history:

```
messages after removing harness entries   6,357   over 7 months
statements written                          178   from 376 readings
scopes                                       13

scope                                  reachable   own   per kind
code.example.com/widget                   116    55        9.2
github.com/chrisguidry/agentic-memory         93    32        6.4
github.com/liken-sh                           81    20        4.0
github.com/chrisguidry/pi-deepinfra           71    10        2.0
```

Reading that history at the rate the writer produces statements gives
about 2,800 statements, so a year of one person's work is thousands of
rows and not millions. Three cuts bring the comparison down to the
`per kind` column:

- **The scope.** A statement can only be replaced by one reachable from
  the place it was said. The scope is a path, so the same index that
  serves a read serves this.
- **The kind.** A prospective statement replaces a prospective
  statement. The kind is a column, so this costs nothing.
- **The message.** Only a message that pushes back on something can
  replace anything. That is the `correction` kind clearing its
  threshold, or `corrects_earlier` scoring 0.70. Measured over 376
  readings, 35 qualify, which is about one in eleven.

So a statement is compared against single digits of other statements,
and the model that names them is the model already writing the
sentence. The comparison costs a few dozen tokens on a call that was
going to happen, and no additional call.

A statement can only be retired by a message that came after the one it
was written from. The queue writes the oldest message first for that
reason. Without the rule, a re-read of last year writes old statements
today, and one of them could retire a statement written from this
morning. A message whose moment is not in the record is offered nothing,
because a statement that cannot be placed in order cannot be replaced in
order.

The list offered to the model is capped, and the newest statements are
offered first. A statement older than the cap cannot be retired, which
is logged when the cap is reached.

## The order a scope reads

The ranking is a weight for the kind, times how fresh the statement is.

```
rank = weight × (floor + (1 - floor) × 0.5 ^ (age in days / half-life))
```

| Kind | Weight | Half-life | Floor |
|---|---|---|---|
| preference | 1.0 | 90 days | 0.6 |
| correction | 1.0 | 90 days | 0.6 |
| semantic | 0.9 | 90 days | 0.6 |
| procedural | 0.9 | 90 days | 0.6 |
| praise | 0.5 | 30 days | 0 |
| prospective | 0.4 | 14 days | 0 |

The floor is the part of a statement's weight that age never takes
away. A kind with a floor is one the record gives no reason to think
has stopped being true, and decaying it to nothing would bury a rule
that has held for a year. A person who writes the same rule into every
session is telling the service that age is not the question.

A kind with no floor is one that expires on its own. A plan describes
work in flight, and work in flight is the thing most likely to have
finished. Praise is about an outcome that is already past. Neither
belongs in front of a session that is starting something, so both fall
out of the list within weeks unless something keeps them fresh.

These numbers are guesses. The only honest way to set them is the
outcome flow, which records whether a turn that received a statement
worked, and that is not built.

The classifier's probability leaves the ranking. It answers whether the
message held a memory of that kind, which is the question that got the
statement written, and once that is settled it says nothing about
whether the statement is worth reading today. It stays in the row and
in the view.

Age is measured from when the message that produced the statement was
said. The recorded time is when the service wrote the statement down, and
a backfill writes a year of statements in a minute, so a ranking on the
recorded time would put a statement from last winter next to one from
this morning. The recorded time stays on the row, because it answers what
the service believed and when, and that is a different question.

## What it does not do

- **It does not match a statement to a prompt.** Recall narrows the
  scope's list to the prompt with a cheap local match, and there is no
  injection point yet, so there is no prompt to match against. The
  ranking serves the whole scope.
- **It does not merge statements that say the same thing.** Two
  statements in different words are two rows with two ranks, and the
  one written later does not retire the one written earlier unless the
  message that produced it pushed back.
- **It does not measure use.** Nothing records how often a statement was
  returned or whether the turn that received it worked, so nothing
  decays a statement that has been handed out a hundred times without
  effect.
- **It does not close a plan when its work finishes.** Nothing in the
  record announces that work completed, so a prospective statement with
  no replacement is retired by the ranking and not by a fact.

## How it is proved

- A retired statement is absent from a read of its scope and from a
  read of every scope above it.
- A retired statement is still in the table, with the pointer that
  retired it and the moment it was retired.
- A message that does not push back on anything is offered no
  candidates, so no statement can be retired by an unrelated one.
- The candidates for a message are the live statements reachable from
  the place it was said, of the kinds that fired, newest first.
- A statement is never offered to a message that came before the message
  it was written from.
- A message whose moment is missing from the record is offered nothing.
- A statement is aged from when its message was said, so a backfill of
  old history does not rank it as new.
- A statement that a newer one replaced is absent from every read and
  every candidate list.
- A statement that holds everywhere is reachable from every scope and
  is a candidate for a message in any of them.
- A statement is filed under the project it names, so a sentence about
  this project is not reachable from a different one.
- A sentence the writer says nothing about stays with the project, which
  is the narrower of the two ways to be wrong.
- The ranking puts a fresh statement of a kind with a floor above the
  same kind aged a year, and puts the same kind aged a year above a
  prospective statement written today.
- Every kind's weight, half-life, and floor are in one table, so
  changing the order is changing one table.
- The whole schema is re-applicable, so a database that already has the
  tables takes the new columns without being rebuilt.

## What the drill measured

Over one machine's history, 223 statements were written, 220 stand, and
3 were retired by a newer statement. All three read as replacements when
they are looked at.

The rule for what replaces what was set twice. The first wording named
five replacements and two of them were wrong: a statement giving an
opinion of a project was taken to replace a statement recording when
that project's repository was created. Saying that two statements must
be unable to both be true, or that the new one settles a question the
old one left open, removed both wrong ones and two right ones. Three
right and no wrong beat three right and two wrong, because a wrong
retirement is silent and the statement is gone from every read.

The gate is 35 of 376 readings, about one in eleven. Those readings were
offered 26 statements each on average at the size the corpus reached,
which is the number the three cuts were meant to bring it to.

A bulk backfill writes the history out of order, because the worker runs
ten tasks at once and the newest messages are then not the last written.
A statement can only be replaced by one written from an earlier message,
so no wrong retirement follows, and most replacements are missed on the
first pass. They happen on the messages that arrive afterwards and on a
re-run. Writing the history in order would need the worker to run one
task at a time, which would set a backfill in front of a live message and
is not worth that.

The list a scope is handed is led by preference and correction, because
those two have the most weight and the highest floor. Semantic and
procedural statements sit under them, and prospective statements fall out
within weeks. That is the shape the weights were chosen for, and at 223
statements it is a strong effect.

## Open questions

- **What the weights should be.** They are set by reading the list.
  Nothing yet acts on a statement, so there is no score to tune.
- **Whether a statement should be retired across scopes.** A decision
  made in one repository can retire the same decision in another only
  if the second message pushes back in the second repository. The
  candidates are drawn from the scope the message was said in.
- **Whether retirement should need the model at all.** The kind and the
  scope narrow the candidates to single digits, and a rule over those
  numbers might do the job without asking.
- **Which moment a statement is aged from.** This uses when the message
  was said. The design keeps a second interval for when the statement was
  true in the world, and nothing records it.
