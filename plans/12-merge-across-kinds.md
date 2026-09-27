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

Plans 10 and 11 were built without changing the classifier's questions. The
statements the old questions wrote as prospective are sorted by plan 11's
backfill, and statements that say the same thing are retired by the merge
pass, so what the re-read was for is covered by two passes of about 1,300
and 870 calls. The re-read below is kept for a later change to the
questions, and it waits for one.

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

During the re-read, a statement written under the new questions must not
retire into one written under the old questions. The new statement is often
the older of the two by the moment its message was said, and step 1 then
ends the old-questions statement it retired into, so nothing live carries
its content. The merge compares statements of one question set during the
re-read.

### Reading the replay after the re-read

Plan 08's replay reads each prompt against the statements that were live
at the moment the prompt was said. The re-read writes its statements after
the week it replays, so under that reading the replay hands out none of
them. The replay gains a mode that reads the store as it is now, with each
statement aged as though it were written at the moment of its message, and
plans 11 and 12 compare before and after in that mode.

## What is built

The merge asks both questions, and a statement retires only into one it was
compared with and only into one that is live. The mark that a statement was
compared is written in the same transaction as its retirements, so a retry
after a failure compares it again. The writer's task embeds, sorts, and
merges on every attempt, so a retry finishes what a failed attempt left.

Merging across kinds is built and off. Over the largest scope, 2,081
statements, a drill judged 15 of 40 sampled merges wrong, 8 of 15 of them
across kinds, and at 0.5 the second question made 7 wrong merges of 25. At
0.6 it makes 1 wrong merge in the sample, and that is its threshold.

The merge pass is built as `agentic-memory-backfill merge` and has not run.
A failure for one statement is counted, the statement stays unmarked, and
the pass goes on, and a second run asks only about what is unmarked. Over
the whole table it asks at most about 870 pairs, at about 710 input tokens
each.

A pass with merging across kinds on retired 223 statements, and a judge
found 11 of 30 sampled merges wrong. In the wrong merges, specific
statements retired into newer general or approving ones, and 17 statements
that were not praise retired into praise. Three rules apply on the write
and in the pass, in one kind and across kinds:

- A statement retires only into one whose kind lasts at least as long, at
  any similarity and under both questions. Correction, preference,
  procedural, and semantic last as long as each other, prospective lasts
  less, and praise least. When the rule kept only praise apart, a pass
  retired 150 statements, and a judge found 10 of 30 sampled merges wrong.
  In that pass, 17 durable statements retired into prospective, where a
  rule fades in weeks or ends when a plan's moment passes. `LASTING` in
  `merge.py` declares the order, because the ranking weighs praise above
  prospective.
- A correction or a preference retires only into a correction or a
  preference. Those are the rules a person states, and in a third pass 8 of
  the 11 wrong merges retired a stated rule into the narrower fact or step
  that applied it once. The rule matters only when merging across kinds is
  on, because otherwise every pair is of one kind.
- A statement retires into one that says the same thing, by the upper
  cutoff or by the first question, only when that one holds all its
  literals, such as a URL, a flag, a path, a file or host name, a name from
  code, or a version. `literals.py` defines what a literal is. A pair the
  first question calls the same thing stands when a literal is lacking,
  whatever the second question says. The second question otherwise does not
  apply this rule, because a newer statement that settles an older one in a
  different way changes a value on purpose, such as a port from 8080 to
  9090. A pair above the upper cutoff that differs in a literal is asked the
  two questions.

Each merge records its run on the statements it retires and compares, and
`agentic-memory-backfill unmerge --run <name>` undoes one run: its retired
statements are live again, except the ones retired into the same text, and
its comparison marks are cleared, so a rerun asks again. `--dry-run` prints
the ids it would change and changes nothing. The runs the writer merges
under are refused: `live`, `write`, `reread`, and `sweep` by name, and any
run with a classify or synthesize call in the ledger, because `/write` and
`/reread` take a run of any name. The merges made before the run was
recorded are found by a window of time, `--since` to `--until`, which also
finds the corrections and the sorts in it, so the window needs both ends,
and the start comes before the end.

### What the passes measured

Three passes ran over the live store on 2026-09-27, each judged by sampling
30 of its merges, and each was undone.

| pass | guards | retired | judged wrong |
|---|---|---|---|
| a | none | 223 | 11 of 30 |
| b | praise and literals | 150 | 10 of 30 |
| c | kinds by how long they last, and literals | 131 | 8 to 11 of 30 |

In pass c, merges within one kind were wrong 6 of 12 times and merges across
kinds 5 of 18, so crossing kinds is not the cause. The question of whether two
statements say the same thing retires a general rule into one instance of it,
and a statement into one that lacks part of what it said. The rule for
stated kinds protects the rules a person gave and leaves those errors within
one kind, which a stricter first question would have to fix. The store holds no
retirement from any pass. The writer still merges each new statement under
the same guards.

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

- **Whether to merge across kinds at all.** A drill over one scope judged 15
  of 40 merges wrong, 8 of 15 of them across kinds: the question of whether
  two statements say the same thing answers 0.53 to 0.72 for pairs that are
  only about the same area, and right merges fall in the same range. The
  merge compares one kind at a time until a question separates them.
- **Whether the survivor should take the kind with the higher weight.** A
  decision that was first written as praise and then as semantic keeps
  semantic, because the newer survives. In the reverse order both stand,
  because a statement retires only into a kind that lasts at least as long.
  A rule written as procedural and then restated in a plan also keeps both,
  so a reader is handed the rule and the plan side by side until the plan
  fades. A plan that settles a rule differently, such as "cut the next
  release from the hotfix branch" against "releases are cut only from main",
  also leaves both live, and they disagree until the plan ends. When the
  plan is a decision, the sort writes it again as a semantic or procedural
  statement before the merge runs, and that statement retires a semantic or
  procedural rule. It does not retire a correction or a preference, which
  retires only into a stated rule, so those two stay live and disagree until
  the person states the change. Whether the newer
  statement should retire into the older one when the older one lasts
  longer is not settled.
