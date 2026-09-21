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

Two passes, because the two jobs have different costs.

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
service. A model call takes about 700 ms, and an ingest cannot wait that
long, so the service schedules the work and never runs it.

## The window

A prompt on its own often means little. Half of the prompts in the
record are under 120 characters, and most of them answer the turn
before. The unit is the exchange: the prompt, and the agent turns
between it and the prompt before it.

The window ends at the prompt being read, because the answer to that
prompt does not exist yet. It reaches back five exchanges by default.

The state carries the two halves apart: `message` is the prompt being
judged, and `before` is the exchanges leading to it. Every question names
`message` in its instructions and says to use `before` only to work out
what the message is replying to.

Keeping them apart is the difference between scoring a message and
scoring a window. Read as one lump, every message inherits whatever was
corrected earlier in its window: a message about a background task scored
0.91 on correction because a correction sat four exchanges before it.
Read apart, the same message scores 0.21.

Measured over one machine's history: 9,140 windows, 1,554 characters at
the median, 3,583 on average, 8,940 at the ninetieth percentile. The
whole history is about 33 MB. Reading all of it again costs little, so
the window size is chosen for what the model needs rather than to save
money.

Two things are left out of a window:

- **Agent records that hold no text.** A turn arrives as many records and
  most are empty. Keeping them would fill the window with blank turns.
- **Entries a harness writes for itself.** Both Claude Code and pi record
  injected skill text, command wrappers, and interrupt markers as though
  the person had typed them. They are a third of the stored prompts and
  none of what the person wanted.

## The kinds

Six questions say what kind of memory is in the message, and five more say
what it is about. Each one asks a single yes/no question, because the model
returns the probability of that proposition and nothing else. Asking about
degree would return the probability of "yes" rather than a degree.

| Kind | The proposition |
|---|---|
| Semantic | Something about the code, the project, or how something works, still true in a month |
| Procedural | How something is done here: a command, a step, a way of working |
| Prospective | Something meant for later, or left unfinished |
| Preference | How one of the speakers wants things done, or something they dislike |
| Correction | One speaker pushing back on what the other did |
| Praise | One speaker approving of the other's work or approach |
| Corrects earlier | The thing being corrected predates this conversation |
| Praise of the outcome | The approval is of the state of things, not of an action |
| About an artifact | The message names a file, a command, or a tool |
| Beyond this project | The statement would hold in other projects too |
| Forbids | The message says not to do something |

The last five are asked on every message rather than only when their kind is
present. A question costs tokens and almost no time, because every question in
a request is answered in one pass, and code decides what was relevant.

Each one separates something the kinds cannot. Forbids averages 0.70 on the
messages that correct something and 0.22 on the rest, and its distribution is
sharply two-peaked, with a median of 0.09 and a ninetieth percentile of 0.96.
Beyond this project averages 0.56 on semantic statements and 0.22 elsewhere.
Praise of the outcome averages 0.49 when praising and 0.09 when not.

Nothing is asked that code can compute exactly. Which speaker said it, and
when, are already in the record, so no question covers them. Nothing is asked
that the record makes unreliable either: dates are text to this model rather
than ordered quantities, so no question asks when something happened.

Three kinds from the psychology of memory are absent.

- **Episodic** is the record. The session is already stored in more
  detail than any memory system holds, so there is nothing to extract.
- **Sensory** has no channel in text. The part that maps onto a codebase
  is the layout of the files, which is the scope, and the scope is
  already derived.
- **Working** is the session in progress.

The provider documents that the answers are independent: one question's
answer is not context for another's. A kind can be added or removed
without changing the others.

The questions name the speakers rather than the person. Both sides state
facts, intentions, and preferences, and the record holds both. Which
speaker said it belongs to the memory, and trust ranks a person's
statement above an agent's.

Correction and praise are the two kinds about the agent's own behavior,
and they are the reason the record is worth reading at all: they say what
to stop doing and what to keep doing. Praise also feeds the outcome
counters, which record how often a turn that received a memory worked.

## The threshold

The reading stores a probability, not a decision about it. The threshold
arrives with the query, so moving it costs a query rather than reading
every window again.

A miss at this stage means no memory, and nothing downstream can recover
it. The classifier is a pure function of the record, and the record is
kept forever, so the history can be read again with different questions.
The whole corpus is 8.2 million tokens, which costs about 35 cents to
read.

The threshold should be set to keep the store small rather than to save
money. Every memory kept is a candidate that recall has to rank and
injection has to weigh. The second pass is cheap as well, so the gate is
there to keep marginal memories out.

A threshold per kind is necessary, because the kinds do not fire at the
same rate. Read over three days of one person's history, 356 messages,
the medians run from 0.09 for praise of an outcome to 0.41 for about an
artifact. At a threshold of 0.5, 260 of the 356 clear it on some kind, so
one number across the kinds filters very little.

How a question is worded moves the distribution more than any threshold
does. Prospective first asked whether the message named something meant
for later, and half the messages qualified, because in a working session
nearly every message names something to do. Asking instead whether the
commitment outlives the conversation moved its median from 0.72 to 0.37
and cut the ones over 0.7 from 181 to 67. Preference moved the same way,
from a median of 0.74 to 0.20. The provider's own guidance says a flat
distribution is usually the criteria being wrong rather than the model,
and this is what that looks like when it is fixed.

## What it does not do

- It writes no memory. There is no memory table yet.
- It calls no large model. Every question goes to one System One model.
- It does not decide what is interesting. It writes probabilities for
  another pass to read.

## How it is proved

- A window carries both sides of the conversation, keeps the message
  apart from the exchanges before it, and reaches back only as far as
  asked.
- Every question inspects `message` and not the window.
- Every question has a column to write to, so adding one means changing the
  schema in the same commit.
- An entry the harness wrote for itself is never scheduled, so a
  scheduled task always has something to read.
- A prompt that arrives twice is read once, because the scheduled work is
  keyed by the entry.
- The reading is written with every kind's probability and with the
  highest one beside them.
- A threshold query returns the reading at a low threshold and not at a
  high one, without the reading being taken again.
- A reading records the fingerprint of the questions it was asked, so
  changing a question does not leave the old answers standing as though
  they were answers to the new one.

## Open questions

- **What the threshold should be.** Nobody knows how good the model is at
  these questions. The provider publishes speed, price, and error rates
  and no accuracy table, so the first measurement is a hand-labelled
  sample of windows rather than a pipeline.
- **Whether the eleven are the right eleven.** They come from what the
  record can support, not from what the second pass needs. A dimension
  earns its place when the thing combining them uses it, and nothing
  combines them yet.
- **How a reading is superseded.** A window grows as a session
  continues, so a reading describes the window as it was when it was
  read. The reading keeps the transcript it saw, and it records a
  fingerprint of the questions it was asked, so a reading under one
  question set is not mistaken for a reading under another. Nothing yet
  re-reads a window that has changed, and nothing re-reads the history
  when a question changes. Both are cheap to add, because the classifier
  is a pure function of the record.
