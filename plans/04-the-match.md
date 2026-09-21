# 04, The match

## The problem

A turn is handed the scope's list, ten at a time, and nothing looks at the
prompt. The first two turns of a session get the statements worth having.
By the seventh the list is at its bottom, every turn costs the same tokens,
and what arrives has nothing to do with what the person typed. Watched over
one session of fourteen prompts, about a third of what was handed over was
useful, a third was a repeat in other words, and a third was a rule from
another project or an instruction an orchestrator wrote for a subagent.

The design says the service narrows the hot set to the prompt with a cheap
match. Nothing defines the match, and nothing builds it.

## The shape

The turn path splits in two, and the service decides which a turn gets.

```
  a session's first prompt                 every prompt after it
        │                                         │
        ▼                                         ▼
  the top of the scope's list            embed the prompt, locally
  ten statements, by kind and age                 │
        │                                         ▼
        │                                nearest live statements
        │                                the session has not seen
        │                                         │
        │                                         ▼
        │                                do the best stand clear
        │                                of the bulk?
        │                                    │           │
        │                                   yes          no
        │                                    │           │
        ▼                                    ▼           ▼
  handed to the turn, recorded        up to five,      nothing
                                      recorded
```

The first form is what runs today, and it stays: a session starts with the
standing rules for where it is. The second form is new. The prompt is
embedded with a small local model, the nearest statements are read from
Postgres, and the turn is handed the few that are about what was typed.
Nothing is the common answer, because most prompts are about the work in
front of the person and the record has nothing to add.

Both forms leave out what the session was already handed, and both record
what went. The clients do not change shape: each sends the prompt with its
ask, and the service decides the form from whether the session has been
handed anything yet.

## The model

Four local models were measured against fourteen prompts from one session
and the 3,149 live statements, one process at a time with four threads.

| model | resident | one prompt | best above the bulk, on a hit | on a miss |
|---|---|---|---|---|
| bge-small-en-v1.5 | 275 MB | 12 ms | 0.08 to 0.11 | 0.04 to 0.07 |
| bge-base-en-v1.5 | 496 MB | 31 ms | 0.09 to 0.14 | 0.07 |
| nomic-embed-text-v1.5 | 852 MB | | 0.05 to 0.16 | 0.05 |
| snowflake-arctic-embed-m | 716 MB | | 0.07 to 0.12 | 0.03 |

All four chose the same best statement whenever one existed, and all four
missed on the same prompts, because nothing relevant existed. Postgres
full-text search matched nothing on any prompt, because a whole prompt
ANDed against one-sentence statements never lands. So the model matters less
than the cutoff, and the smallest one is enough. It runs through fastembed
on ONNX, on CPU, with no service outside the compose stack.

The model is a setting, with its threads, so a larger one is one line.

## The cutoff

The last column of the table is the finding. On a real hit the best match
stands clear of the crowd, and on a miss it does not, whatever its absolute
score. So the cutoff is on separation: a statement is handed over when its
similarity exceeds the ninety-ninth percentile of the scope's similarities
by a margin, and the margin is a setting. A fixed score would drift with
the model and the corpus, and this does not.

The rank a statement is handed at is its similarity times the weight the
kind and the age already give it, so a fresh correction outranks an old
plan that matches the same words.

## Where the embedding lives

A statement is embedded once, by the worker, when the writer writes it, and
the vector is a column on the row. Postgres holds it through pgvector, so
the nearest statements are one query, and the store keeps the model's name
beside the vector, because a vector from one model means nothing to
another. Changing the model re-embeds the table, which is a task over
thousands of rows and takes minutes.

The service embeds the prompt at turn time. That is a model on the turn
path, which the design forbids for the models it was thinking of. This one
takes twelve milliseconds and holds nothing outside the process, so the
150 ms deadline still holds with room, and the rule the deadline protects
is kept.

## What it does not do

- **It does not merge statements that say the same thing.** Five near
  matches can be five wordings of one rule. That is plan 03's open item and
  it is worse now, because a match finds every wording at once.
- **It does not weigh who said it.** A subagent's instruction that matches
  the prompt is handed over beside the person's own words, labelled but not
  discounted.
- **It does not learn from what worked.** The handout is recorded and
  nothing reads it back yet.

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
- The model's name, its threads, the cutoff, and both limits are settings.
- The ask answers inside the deadline against the compose stack, measured.
- The suite runs against pgvector, and the whole schema is re-applicable.

## Open questions

- **Whether the session-start form should stay.** If the match is good,
  the standing rules for a scope could arrive as matches too, and the
  first turn would cost nothing more than any other.
- **What the margin should be.** It is set by reading fourteen prompts. The
  injections table now records every handout, so the honest setting comes
  from a labelled sample of what was handed and whether it helped.
- **Whether a prompt should match the exchange rather than itself.** A
  reply of "yes, that one" matches nothing on its own, and the window the
  classifier reads exists for the same reason.
