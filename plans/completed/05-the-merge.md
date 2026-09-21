# 05, The merge

Closed 2026-09-21. Built in the commit that built it, and closed after.

## The problem

The table holds the same rule many times in different words. Of 3,158 live
statements, 141 have a neighbour of the same kind at a cosine similarity of
0.98 or more, and 333 at 0.90 or more. "Commits are never amended" is in
the table twenty times.

The match in plan 04 makes this worse than it was. The cutoff there is a
margin over the scope's ninety-ninth percentile, and twenty wordings of one
rule are the top twenty matches for any prompt about commits, so the
baseline is itself a duplicate and nothing passes. A session's opening list
is the same: ten slots, and eight went to one fact.

The design lists merging under maintenance, and plan 03 lists it as undone.

## The shape

A statement is written, embedded, and then compared with its neighbours.

```
  a statement is written and embedded
        │
        ▼
  the nearest live statements of the same kind, in the same scope
        │
        ├── similarity at or above the upper cutoff ──► superseded by the new one
        │
        ├── between the two cutoffs ──► ask Jev: do these say the same thing,
        │                               and does neither forbid what the other
        │                               allows?
        │                                   │            │
        │                                  yes           no
        │                                   │            │
        │                                   ▼            ▼
        │                          superseded by     both stand
        │                          the new one
        │
        └── below the lower cutoff ──► nothing
```

The survivor is the statement said most recently, and the others take the
pointer plan 03 built: `superseded_by` names the survivor and
`superseded_at` records the moment. Nothing is deleted or rewritten. How
many times a rule was said is the count of rows that point at the survivor,
which a later ranking can read.

The upper cutoff is where two statements are one sentence with a word
moved. Read over the table, that is 0.95. Below it, and down to a lower
cutoff, an embedding cannot tell a negation from its opposite: "/nix/ is
not the docketeer data directory" and "the docketeer data directory is
/docketeer/" score 0.935, and those two agree, but the next pair at that
score may not. So the band is asked, and the asker is the System One model
the classifier already uses, because "do these two say the same thing" is
one yes/no proposition. It runs in the worker, off the turn path, where its
latency does not matter.

Both cutoffs are settings.

## The boundary

A statement is compared only with statements of its own kind in its own
scope, where a null scope is its own scope. "Tests come before code in
liken-sh" and the same rule in liken are one rule at two places, and
merging them would move where the rule applies. Plan 03's open question on
retiring across scopes is still open, and this plan does not answer it.

## When it runs

Once over the backlog, as a task the service can schedule, oldest statement
first, so the survivor of each group is the newest. Then on every write,
after the worker embeds the new statement. A statement whose embedding is
from another model is not compared, because its similarity means nothing.

## What it does not do

- **It does not merge across scopes.** See above.
- **It does not rewrite.** The survivor is one of the existing wordings.
- **It does not rank by how often a rule was said.** The count is
  readable from the table, and nothing reads it yet.

## How it is proved

- Two statements of one kind in one scope at or above the upper cutoff
  leave one standing, the newer, and the older names it.
- Two statements in the band leave one standing when the model says they
  say the same thing, and both standing when it says they do not.
- Two statements below the lower cutoff both stand, and the model is not
  asked.
- Two statements of different kinds, or in different scopes, both stand
  whatever their similarity.
- A statement retired by a merge is absent from every read and from the
  match, the same as one retired by a correction.
- A statement embedded by another model is never a candidate.
- The backlog pass over the table leaves no pair of live same-kind,
  same-scope statements above the upper cutoff.
- Both cutoffs are settings.

## Open questions

- **Whether the count should rank.** A rule said twenty times is a rule
  the person keeps having to repeat. That could raise it, or it could mean
  the injection is not landing.
- **What the lower cutoff should be.** Pairs at 0.80 to 0.85 are mixed,
  and the model's answers on that band are the way to set it.

## What the drill measured

The pass ran against the compose stack over 3,170 live statements, every
one embedded with bge-small. It compared each live statement with its
same-kind, same-scope neighbours and retired 383 into a newer one, leaving
2,795 standing. Eight statements were written while it ran.

317 pairs of live statements sat at or above the upper cutoff of 0.95
before the pass, and none did after. That is what the pass is for: the
match cannot pass a rule that is its own baseline, and the baseline no
longer holds duplicates.

1,221 pairs sat in the band between 0.80 and 0.95, and the System One
model was asked about each. The pass took 242 seconds and about 1,800
model calls, which is the cost of asking rather than guessing in the band.
The lower cutoff stays 0.80 for now. The model's answers in the band were
not counted by similarity, so the open question above is still open.

The cutoffs are settings, so the next pass can be narrowed by raising the
lower one without a code change.
