# 04, The match

## The problem

A turn is handed the scope's list, ten at a time, and nothing reads the
prompt. The first two turns of a session get the statements worth having.
By the seventh turn the list is at its bottom, every turn costs the same
tokens, and what arrives has nothing to do with what the person typed.
Over one session of fourteen prompts, about a third of what was handed
over was useful, a third repeated an earlier statement in other words, and
a third was a rule from another project or an instruction an orchestrator
wrote for a subagent.

The design says the service narrows the hot set to the prompt with a cheap
match. Nothing defines the match, and nothing builds it.

## The shape

The turn path has two forms. A session's first prompt gets one form, and
every prompt after it gets the other.

```
  a session's first prompt                 every prompt after it
        │                                         │
        ▼                                         ▼
  the top of the scope's list            embed the prompt, locally
  ten statements, by kind and age                 │
        │                                         ▼
        │                                the nearest live statements
        │                                the session has not seen
        │                                         │
        │                                         ▼
        │                                is the best one well above
        │                                the rest of the scope?
        │                                    │           │
        │                                   yes          no
        │                                    │           │
        ▼                                    ▼           ▼
  handed to the turn, recorded        up to five,      nothing
                                      recorded
```

The first form is what runs today, and it stays: a session starts with the
standing rules for where it is. The second form is new. The service embeds
the prompt with a small local model, reads the nearest statements from
Postgres, and hands the turn the few that are about what was typed.
Nothing is the common answer, because most prompts are about the work in
front of the person and the record has nothing to add.

Both forms leave out what the session was already handed, and both record
what went. The clients do not change shape. Each sends the prompt with its
ask, and the service picks the form: the first form when the session has
been handed nothing yet, and the second form after that.

## The model

Four local models were measured against fourteen prompts from one session
and the 3,149 live statements, one process at a time with four threads.

| model | resident memory | one prompt | best score above the 99th percentile, on a hit | on a miss |
|---|---|---|---|---|
| bge-small-en-v1.5 | 275 MB | 12 ms | 0.08 to 0.11 | 0.04 to 0.07 |
| bge-base-en-v1.5 | 496 MB | 31 ms | 0.09 to 0.14 | 0.07 |
| nomic-embed-text-v1.5 | 852 MB | not re-timed | 0.05 to 0.16 | 0.05 |
| snowflake-arctic-embed-m | 716 MB | not re-timed | 0.07 to 0.12 | 0.03 |

All four chose the same best statement whenever one existed, and all four
missed on the same prompts, because no relevant statement existed. Postgres
full-text search matched nothing on any prompt, because it requires every
word of a whole prompt to appear in one sentence. So the model matters less
than the cutoff, and the smallest one is enough. It runs through fastembed
on ONNX, on the CPU, with no service outside the compose stack.

The model name and its thread count are settings, so a larger model is one
line.

## The cutoff

The last two columns of the table are the finding. On a real hit, the best
match scores well above the rest of the scope. On a miss, it does not,
whatever its absolute score. So the cutoff is on the margin, not the score:
a statement is handed over when its similarity exceeds the ninety-ninth
percentile of the scope's similarities by a set amount. The amount is a
setting. A fixed score would move with the model and the corpus, and a
margin does not.

In a scope of a few thousand statements the percentile has about thirty
above it. In a scope of a hundred it would have one, and the few wordings
of one rule would then hide each other, so the baseline never rises above
the tenth best. A scope of ten statements or fewer has no baseline, and
its prompts are handed nothing.

The rank a statement is handed at is its similarity times the weight the
kind and the age already give it, so a fresh correction ranks above an old
plan that matches the same words.

## Where the embedding is stored

The worker embeds a statement once, when the writer writes it, and stores
the vector in a column on the row. Postgres holds it through pgvector, so
the nearest statements are one query. The row also holds the model's name,
because a vector from one model means nothing to another. Changing the
model re-embeds the table, which is a task over thousands of rows and
takes minutes.

The service embeds the prompt at turn time. That is a model call on the
turn path, which the design rules out for models that answer over a
network. This one runs in the process, takes twelve milliseconds, and
needs nothing outside it, so the deadline of 150 milliseconds holds with
room.

## What it does not do

- **It does not merge statements that say the same thing.** Five near
  matches can be five wordings of one rule. Plan 03 lists the merge as
  undone, and a match makes the gap wider, because it finds every wording
  at once.
- **It does not weigh who said it.** A subagent's instruction that matches
  the prompt is handed over beside the person's own words, labelled and
  not discounted.
- **It does not read what worked.** The handout is recorded and nothing
  reads it back yet.

## How it is proved

- A session's first ask is handed the top of the scope's list, and its
  later asks are not.
- A later ask with a prompt that matches a statement is handed that
  statement, and one that matches nothing is handed nothing.
- A statement the session was already handed is never handed again, in
  either form.
- The per-prompt handout never exceeds its limit, and the session-start
  handout never exceeds its own.
- A statement with no embedding, or one embedded by another model, is not
  a candidate for the match.
- The model's name, its threads, the margin, and both limits are settings.
- The ask answers inside the deadline against the compose stack, measured.
- The suite runs against pgvector, and the whole schema is re-applicable.

## Open questions

- **Whether the session-start form should stay.** If the match works, the
  standing rules for a scope could arrive as matches too, and the first
  turn would cost no more than any other.
- **What the margin should be.** It was set by reading fourteen prompts.
  The injections table records every handout, so a labelled sample of what
  was handed and whether it helped is the way to set it.
- **Whether a prompt should match the exchange rather than itself.** A
  reply of "yes, that one" matches nothing on its own. The classifier reads
  a window for the same reason.
