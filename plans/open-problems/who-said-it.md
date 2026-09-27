# Who said it

## The problem

Two live statements can disagree, and a session that is handed both has no
way to tell which to believe. Validation found one such pair: a statement
from an agent at depth one says a file can name two items, and a statement
the person made minutes earlier says a file belongs to exactly one. Both
were live, and the agent's statement matched the prompt more closely.

The merge does not settle it. The two were said minutes apart, and neither
settles the other by being newer, because the newer one is the agent's.

## What is known

- Every statement records who said the message it came from and how far
  from the person: `actor` and `actor_depth`. The design says trust ranks
  a person's statement above an agent's, and nothing reads the two columns
  yet.
- A depth-one statement is an instruction an orchestrator wrote for a
  subagent. It is often a paraphrase of the person, and sometimes the
  agent's own guess.
- The ranking weighs a statement by its kind and its age. The match weighs
  it by similarity times that weight.

## What would settle it

A decision on where trust applies:

- in the ranking and the match, as a weight that puts the person's
  statement above an agent's when both are handed
- in the merge, so a person's statement is never retired into an agent's,
  and an agent's statement that contradicts a person's is retired into it
- or at write time, so an agent's statement is written only when nothing
  the person said already covers it

and a labelled set of contradicting pairs to measure it against.
