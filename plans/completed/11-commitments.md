# 11, Commitments

Closed 2026-09-27.

## The problem

The prospective question asks whether a message settles something, defers
something, or leaves a question open, in a way later work has to respect.
That question fires on most of a working session. From 2026-09-25 to
2026-09-27, prospective was about 60 percent of the statements written.

Most of them are one of two things, and neither belongs in the kind:

- **The state of the work.** A file is uncommitted, a branch is three
  commits ahead, a build is waiting on CI. This is true for an hour. The
  record holds it already, and the next session reads it from git.
- **A decision.** A field is an enum of two values, a component owns a
  piece of data. This is true until someone changes it, which is how a
  semantic or procedural statement behaves, and the ranking gives
  prospective a half-life of 14 days and no floor, so a decision filed as
  prospective fades out of the ranking while it is still true.

What is left is the kind's real content: a commitment with a condition
that ends it. "Do not release before Monday." "No new upstream pull
requests until the open two get a response." "Hold the migration until the
person says go." Nothing ends these when the condition is met. Plan 03
names the gap: a prospective statement with no replacement is retired by
the ranking and not by a fact.

## The shape

The kind keeps its name and narrows to commitments. Each part of what it
catches today goes somewhere definite.

| the message holds | today | after |
|---|---|---|
| a commitment that ends on a condition | prospective | prospective, with its condition |
| a decision | prospective | semantic or procedural |
| the state of the work | prospective | nothing |

### The sort

The classifier's prospective question stays as it is. It catches decisions,
commitments, and the state of the work, and it catches them well. What goes
wrong is that everything it catches is written as prospective.

So each prospective statement is sorted after the writer writes it, by one
yes-or-no question to Jev per class, asked about the statement and the
exchange it came from:

- Does the statement hold until someone changes it? A yes is a decision, and
  the statement is written as semantic or procedural.
- Is the statement only the state of the work, such as uncommitted files or a
  build waiting on CI? A yes ends the statement.
- What is left is a commitment, and Jev is asked whether it ends at a moment,
  and at which one, or on an event.

Jev answers one proposition at a time, and the classifier and the merge show
it does that reliably. The sort then does not depend on the writer following
a format.

### The condition

A condition is one of two forms:

- **a moment**: the statement ends when the moment passes
- **an event**: the statement ends when a later message reports it

A statement whose condition is a moment ends without anything else
happening. A statement whose condition is an event ends through the path
plan 03 built for replacement: when the writer writes from a later message
in the same scope, the live commitments of the scope are offered to it, and
it names any whose condition the message meets.

A moment is said in the person's time zone, which is a setting, so "the
morning" resolves where the person is.

### Ending without a survivor

Plan 03 retires a statement by pointing it at the statement that replaced
it. A commitment that ends has no successor, so a statement can now end on
its own. The row records when it ended and why: a moment passed, or which
message met its condition. A statement that ended is absent from every read,
the same as one that was replaced.

## The contracts

- `memories` gains the condition, as a moment or as the text of the event.
- `memories` gains the end of a statement without a successor, with its
  reason, and the live index and every read treat an ended statement as
  retired.
- The classifier's questions do not change, so no re-read is forced by this
  plan.

## What it does not do

- **It does not re-read the corpus.** The re-read is expensive, and plan 12
  also needs one, so plan 12 runs one re-read under both plans' changes.
- **It does not retire today's prospective statements.** Plan 12 does, as
  part of the re-read.

## How it is proved

- A labelled set of prospective statements from the last week, sorted by
  hand into commitment, decision, and state of the work, with invented
  wording in the fixtures. The sort puts each one where the table says, and
  the drill records the share that land right.
- A commitment with a moment is absent from reads after the moment.
- A commitment with an event is ended by a later message that reports the
  event, and is not ended by one that does not.
- The replay under plan 08 shows prospective's share of new statements
  over the same week, before and after.

## What was tried

Two sorts were measured on 80 prospective statements from a week of real
work, sorted by hand into 9 commitments, 42 decisions, and 29 statements of
the state of the work.

Narrowing the classifier's questions landed 40 of the 80 where the table
puts them, and lost 17 of the decisions and 6 of the commitments outright.
The classifier judges one message, and a decision often scores just below
the semantic and procedural thresholds.

Asking the writer to name a sort before its sentence landed 34 of the 80.
In 23 replies the writer gave no sort, or gave it as a kind, and more
wording over four rounds did not change that. When it did give a sort, it
named 5 of 7 commitments, 18 of 30 decisions, and 3 of 20 statements of
the state of the work.

## What the drill measured

The sort asks Jev five questions about each prospective statement in one
call: whether it is a commitment, a decision, or the state of the work,
whether a decision is semantic or procedural, and when a commitment ends.
The last is a choice among moments computed from when the message was said
in the person's zone: the end of that day, the next morning at 09:00, the
end of the next day, the next Monday at 09:00, an event, or none.

On the same 80 hand-sorted statements, the sort landed 61 where the table
puts them, against 40 for the narrower questions and 34 for the writer's
sort. Across the last two rounds, all 42 decisions became semantic or
procedural and none ended.

| sorted by hand | what the sort did |
|---|---|
| 9 commitments | 1 with a moment, 7 with an event, 1 ended |
| 42 decisions | 32 semantic, 10 procedural |
| 29 state of the work | 11 ended, 12 kept as decisions, 6 kept as commitments |

The state of the work is where it errs, and it errs toward keeping. At a
decision threshold of 0.55 the sort starts to end decisions, so the
threshold stays at 0.5.

A commitment said at 00:17 on a Sunday not to act before the next morning
resolved to 09:00 on Monday. "Tomorrow morning" said at 23:30 on a Saturday,
when UTC is already Sunday, resolved to 09:00 on Sunday in the person's zone.

A decision becomes a new semantic or procedural row, and the prospective row
retires into it. When the message already has a statement of that kind, the
prospective row stays prospective, because retiring it into that statement
would retire it into a different sentence. The claim, the new row, and the
retirement run in one transaction, and a statement a backfill listed but
that something else retired or sorted since is left alone.

Sorting the 1,307 live prospective statements takes about 4.4 million input
tokens, because each call carries the exchange the statement came from.

## Open questions

- **What ends a commitment on its event.** The event is stored as the
  statement's own words, and nothing reads it yet. An event is usually
  reported by the agent, and the writer reads only the person's prompts, so
  the writer's replacement path rarely sees one. A commitment with an event
  fades by age until this is built.

- **Whether a commitment with neither form should be written.** "Revisit
  this someday" has no condition. Writing it with no end makes it permanent,
  and not writing it loses it.
