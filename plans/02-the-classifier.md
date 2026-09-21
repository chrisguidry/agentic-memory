# 02, The classifier

## The problem

The record holds everything a person and their agents said. Nothing
reads it, so nothing becomes memory.

Deciding what is worth remembering is the expensive part. It needs a
model, and reading every window at full strength costs more than the
record is worth. The record also holds more than memory: a third of what
arrives as a prompt is the harness talking to itself.

This plan builds the first of two passes. It reads a window, asks a
cheap model which kinds of memory are in it, and writes down the
probabilities. It writes no memory. The second pass reads what this one
scored highly, and is a later plan.

## The shape

Two passes, because the two jobs are different sizes.

```
  a prompt arrives
        │
        ▼
  the service writes the record ──► the docket
                                        │
                                        ▼
                              ┌───────────────────────┐
                              │  the first pass       │
                              │  read the window      │
                              │  score five kinds     │
                              │  write probabilities  │
                              └───────────┬───────────┘
                                          │
                          probabilities, above a threshold
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │  the second pass      │  a later plan
                              │  a larger model       │
                              │  writes a memory      │
                              └───────────────────────┘
```

The first pass runs in a worker, which is a separate process from the
service. A model call that takes a second cannot hold up an ingest, so
the service schedules work and never runs it.

## The window

A prompt read on its own is close to meaningless. The person is replying
to something, and what they are replying to is where the meaning is. So
the unit is the exchange: the prompt, and the agent turns between it and
the prompt before it.

The window ends at the prompt being read, because the answer to that
prompt does not exist yet. It reaches back five exchanges by default.

Measured over one machine's history: 9,140 windows, 1,554 characters at
the median, 3,583 on average, 8,940 at the ninetieth percentile. The
whole history is about 33 MB, so reading all of it again is cheap and the
window size is a quality decision rather than a cost one.

Two things are left out of a window:

- **Agent records that hold no text.** A turn arrives as many records and
  most are empty, so an unread window would otherwise be mostly blank
  turns.
- **Entries a harness writes for itself.** Both Claude Code and pi record
  injected skill text, command wrappers, and interrupt markers as though
  the person had typed them. They are a third of the stored prompts and
  none of what the person wanted.

## The kinds

Five questions, one per kind of memory the record can hold. Each asks
about a single proposition, because a yes/no answer is the probability of
that proposition and nothing else. A question about degree comes back as
the probability of "yes" and does not measure the degree.

| Kind | The proposition |
|---|---|
| Semantic | Something about the code, the project, or how something works, still true in a month |
| Procedural | How something is done here: a command, a step, a way of working |
| Prospective | Something meant for later, or left unfinished |
| Preference | How the person wants things done, or something they dislike |
| Correction | The person pushing back on what the agent did |

Three kinds from the psychology of memory are deliberately absent.

- **Episodic** is the record. The session is already stored in more
  detail than any memory system holds, so there is nothing to extract.
- **Sensory** has no channel in text. The part that maps, the layout of
  things in space, is the scope, and the scope is already derived.
- **Working** is the session in progress.

The answers are independent by construction, so one kind's answer is not
context for another's and a kind can be added or removed without changing
the rest.

## The threshold

The reading stores a probability, not a decision about it. The threshold
arrives with the query, so moving it costs a query rather than reading
every window again.

This matters more than it looks. A miss at this stage means no memory,
and nothing downstream can recover it. But the classifier is a pure
function of the record, and the record is kept forever, so a miss is a
temporary loss: the whole history can be read again with different
questions for the price of a coffee.

That makes the gate a quality filter rather than a cost filter. Every
memory kept is a candidate that recall has to rank and injection has to
weigh, and a store full of marginal memories makes the briefing worse.
The threshold should be set to keep the store small, not to save money.

## What it does not do

- It writes no memory. There is no memory table yet.
- It calls no large model. Every question goes to one System One model.
- It does not decide what is interesting. It writes probabilities, and
  something else decides.

## How it is proved

- A window carries both sides of the conversation, ends at the prompt
  being read, and reaches back only as far as asked.
- An entry the harness wrote for itself never reaches a model.
- A prompt that arrives twice is read once, because the scheduled work is
  keyed by the entry.
- The reading is written with every kind's probability and with the
  highest one beside them.
- A threshold query returns the reading at a low threshold and not at a
  high one, without the reading being taken again.

## Open questions

- **What the threshold should be.** Nobody knows how good the model is at
  these questions. The provider publishes speed, price, and error rates
  and no accuracy table, so the first measurement is a hand-labelled
  sample of windows rather than a pipeline.
- **Whether the five kinds are the right five.** They come from what the
  record can support, not from what the second pass needs.
- **How a reading is superseded.** A window changes as a session grows,
  so a reading taken at one moment describes a window that no longer
  exists. The reading keeps the transcript it saw, and nothing yet
  re-reads a window that has moved.
