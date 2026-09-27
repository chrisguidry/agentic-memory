# 11, Commitments

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

### The questions

The prospective question asks whether the message commits the speakers to
do or not do something until a condition holds: a date, an event, or a
person's word. The state of the work is named in its criteria as a no.

The semantic and procedural questions name a decision in their criteria as
a yes, so a message that settles a design is still read.

### The condition

When the writer writes a prospective statement, it also writes the
condition that ends it, in the same call. A condition is one of two forms:

- **a moment**: the statement ends when the moment passes
- **an event**: the statement ends when a later message reports it

A statement whose condition is a moment ends without anything else
happening. A statement whose condition is an event ends through the path
plan 03 built for replacement: when the writer writes from a later message
in the same scope, the live commitments of the scope are offered to it, and
it names any whose condition the message meets.

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
- The new questions have a new fingerprint, so their readings and their
  statements are written beside the old ones. Plan 12 runs the re-read.

## What it does not do

- **It does not re-read the corpus.** The re-read is expensive, and plan 12
  also needs one, so plan 12 runs one re-read under both plans' changes.
- **It does not retire today's prospective statements.** Plan 12 does, as
  part of the re-read.

## How it is proved

- A labelled set of prospective statements from the last week, sorted by
  hand into commitment, decision, and state of the work, with invented
  wording in the fixtures. The new questions put each one where the table
  says, and the drill records the share that land right.
- A commitment with a moment is absent from reads after the moment.
- A commitment with an event is ended by a later message that reports the
  event, and is not ended by one that does not.
- The replay under plan 08 shows prospective's share of new statements
  over the same week, before and after.

## Open questions

- **Whether a commitment with neither form should be written.** "Revisit
  this someday" has no condition. Writing it with no end makes it permanent,
  and not writing it loses it.
