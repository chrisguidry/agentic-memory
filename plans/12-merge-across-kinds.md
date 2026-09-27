# 12, Merge across kinds

## The problem

Plan 05 compares a statement only with neighbours of its own kind in its
own scope. One decision is often written twice from one conversation, once
as praise when the person approves a shape and once as prospective or
semantic when the shape is settled. The two wordings never meet, so both
stay live, and when the second changes the decision, the first still
states the old one.

Plan 05's question also asks only whether two statements say the same
thing. A newer statement that changes an older one answers no, and both
stand. A reader is then handed two statements that disagree, with nothing
to say which is current.

Plans 10 and 11 change the questions, and the corpus was written under the
old ones. A statement the new questions would not write, such as the state
of the work, stays live until something retires it.

## The shape

### The merge

A statement is compared with its nearest live neighbours in its own scope,
of every kind. The two cutoffs from plan 05 stay, and Jev is asked two
questions about a pair in the band:

- whether the two say the same thing, as today
- whether the newer one settles the same question as the older one, in a
  different way

A yes to either retires the older statement into the newer one through
`superseded_by`. The survivor keeps its own kind.

The scope boundary stays where plan 05 put it: a rule in two places is two
statements.

### The re-read

After plans 10 and 11 are built, the classifier and the writer read the
whole record again under the new questions, as the run `reread`. The new
statements are written beside the old ones, because the question
fingerprint is part of the key. Then:

1. Each old statement ends. When the new questions wrote a statement of
   the same kind from the same message, the old one points at it. When
   they wrote nothing from that message, the old one ends with no survivor
   and the reason `reread`, through the column plan 11 adds.
2. The merge runs over every live statement, oldest first, so each
   statement meets the ones said before it.
3. The backfill from plan 10 answers the new question for every live
   statement.

Nothing is deleted. `otel_exports` and `logs` are not touched. An old
statement is still in the table, with the pointer or the reason that ended
it, so the step is reversible by clearing those columns.

### Reading the replay after the re-read

Plan 08's replay reads each prompt against the statements that were live
at the moment the prompt was said. The re-read writes its statements after
the week it replays, so under that reading the replay hands out none of
them. The replay gains a mode that reads the store as it is now, with each
statement aged as though it were written at the moment of its message, and
plans 11 and 12 compare before and after in that mode.

## The contracts

- The merge's second question has its own threshold, which is a setting.
- The re-read is a named run in the ledger, so its cost totals separately
  from live use.
- The re-read runs against the compose stack with a copy of the homelab
  store before it runs on the homelab, and its cost estimate and its
  replay diff are approved by the person first.

## How it is proved

- Two statements of different kinds that say the same thing merge, and
  the newer survives.
- A newer statement that changes an older one retires it, and one that
  adds a detail without changing it merges as today.
- Two statements that disagree in two different scopes both stand.
- After the re-read on the copy, the replay under plan 08 shows the share
  of handed pairs labelled wrong, before and after, over the same week.
- Clearing the end columns of the statements the re-read ended restores
  the table as it was.

## Open questions

- **Whether the survivor should take the kind with the higher weight.** A
  decision that was first written as praise and then as semantic keeps
  semantic, because the newer survives. The reverse order would keep
  praise, and the ranking weighs praise at half.
