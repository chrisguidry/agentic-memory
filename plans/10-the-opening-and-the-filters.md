# 10, The opening and the filters

## The problem

A session's first prompt is handed the top ten statements of its scope,
ranked by kind and age, and the prompt is not read. Statements with no
scope are reachable from every scope, and a preference or a correction has
the highest weight, so the same few statements with no scope open nearly
every session, whatever it is about. Over the week after 2026-09-23, first
prompts took 250 of the 380 statements handed out, and 40 percent of the
380 had no scope. The most frequent statement with no scope went to 21
sessions, and it concerns one kind of
request.

Later prompts are matched, and the match hands out a statement when it
scores well above the rest of the scope. A short reply to the agent, such
as "keep going" or "looks good", scores well above the rest against a
statement of praise, because the two are written in the same register. So
those replies are handed a statement about some earlier piece of work being
received well, which helps no one.

The bastion also asks for recall on prompts the classifier skips as
plumbing: a task notification, a skill's body, or feedback from a Stop hook.
Each of those is handed statements, and a statement handed to one is marked
seen and is never handed to that session again.

Plan 04 left the opening list as an open question, and noted that a reply
like "yes, that one" matches nothing useful on its own.

## The shape

No model is called on the turn path. The two filters below read answers
the worker wrote earlier, off the turn path, with Jev, the System One model
the classifier already uses. The turn path compares embeddings against
those answers, which is one more indexed query.

```
  a prompt arrives
        │
        ▼
  embed the prompt, locally
        │
        ▼
  are its nearest classified prompts mostly empty? ── yes ──► nothing
        │ no
        ▼
  match among statements that may be handed out
        │
        ▼
  first prompt of the session? ── yes ──► add up to 3 from the scope's list,
        │ no                               statements with a scope only
        ▼                                          │
  handed to the turn, recorded ◄───────────────────┘
```

### Which statements may be handed out

When the writer writes a statement, Jev answers one more question about
it: would an agent starting new work in this place act differently for
knowing this? A standing rule, a fact about the code, and a correction
answer yes. A remark that one piece of work went well answers no. The
answer is a probability on the row, like the classifier's kinds.

The match reads only statements whose answer clears a threshold. A
statement below it stays live, and the ranking, the merge, and the outcome
flow still read it.

The statements already in the table get the answer through a backfill:
one Jev call per live statement, about 3,200 calls. The ledger prices the
backfill before it runs.

### Which prompts are matched

The classifier already reads every real prompt and answers each kind's
question about it. A prompt whose answers are all below their thresholds
holds no memory, and most of those are replies that carry no subject of
their own. The worker embeds each prompt it classifies and stores the
vector with the reading.

At the turn, the prompt's vector finds its nearest classified prompts.
When the share of them that held no memory is above a cutoff, the turn is
handed nothing. The reference set grows as the classifier reads, so it
follows the way the person writes without anyone listing phrases.

### The opening list

A session's first prompt goes through the match like every other prompt.
The match finds what the first prompt is about, and a statement with no
scope reaches a session only through the match.

The first prompt also gets up to three statements from the scope's list,
in the order the list has now. Only statements with a scope are candidates,
so the opening carries the standing rules for where the session is, and
nothing that holds everywhere.

### Prompts that are plumbing

The turn path applies the classifier's plumbing filter before anything else,
so a prompt the classifier would not read is handed nothing and marks
nothing seen.

## The contracts

- `memories` gains a column for the answer to the new question, and the
  match reads it through the live index.
- The reading gains the prompt's embedding and the embedding model's name,
  and the nearest readings are one indexed query.
- The threshold on the new question, the size of the neighbourhood, the
  cutoff on its empty share, and the size of the opening list are settings.

## What it does not do

- **It does not call a model on the turn path.** The design rules that
  out, and plan 04 kept it out.
- **It does not change what the writer writes.** Plan 11 changes the
  questions. This plan only adds one question about the statement that was
  written.

## How it is proved

- Every threshold and cutoff is set from a replay under plan 08, over the
  same week, with the labels. The drill records the values and why.
- The replay shows the share of handed statements with no scope falling
  from 40 percent to the share the match earns on its own.
- The replay shows fewer handed pairs labelled noise on short replies, and
  no loss of the pairs labelled good.
- A prompt whose neighbourhood is mostly empty is handed nothing, and a
  prompt with a subject beside it is matched.
- A first prompt is matched, and the three from the list all have a scope.
- A task notification or a skill's body is handed nothing, and marks
  nothing seen.
- The recall histograms from plan 08 show the new query's cost, and the
  turn path stays inside the bastion's deadline.

## Open questions

- **Whether a reply should borrow the subject of the exchange.** A reply
  with no subject gets nothing under this plan. Matching the last exchange
  instead of the reply is the other answer, and the replay can compare the
  two.
