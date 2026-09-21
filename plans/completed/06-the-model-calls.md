# 06, The model calls

Closed 2026-09-21. Built in the commit that built it, and closed after.

## The problem

The worker calls a model in three places. The classifier asks the System
One model which kinds of memory are in a message, the merger asks it
whether two statements say the same thing, and the writer asks a
DeepInfra model for the sentence. Each answer comes back with a model
name and a token count, and the code reads the answer and drops the
metadata.

A deep backfill makes thousands of these calls. Nothing in the store
answers how many, which model, or which task caused them. The provider
dashboard shows a total and nothing about the messages, the tasks, or the
run.

Ongoing use has the same hole. A day of live capture calls the classifier
for every message and the writer for the messages that scored highly, and
no number says how much work that was.

The harness telemetry path already records tokens and cost, but those
rows come from the harness and describe the harness's own calls. The
service's calls happen where no harness sees them.

## The shape

One table, `model_calls`, and one row for each call the worker makes.

```
  a docket task runs
        │
        ▼
  the worker calls a provider ──► the answer, or the refusal
        │                              │
        │                              ▼
        │                        one row in model_calls:
        │                        provider, model, task, run,
        │                        tokens, duration, outcome
        ▼
  the task reads the answer and writes its own row
  (a classification, a statement, or a merge)
```

The write happens where the call happens, at the client boundary, so a
task cannot forget it and a new call site is recorded without doing
anything. The task names what the call was for, and the ledger writes the
row.

Two clients make the calls. The System One client is the TypeSafe SDK,
shared by the classifier and the merger. The writer is an `httpx` client
that posts to DeepInfra. The ledger wraps both.

## What a row holds

- **When.** `called_at`, the moment the call was made.
- **Who answered.** `provider`, `model`, and `request_id`. The model is
  the versioned id the provider reports, not the alias that was asked
  for, because the alias moves. `jev-latest` today answers as
  `jev-1.13.0`.
- **What it was for.** `task` is `classify`, `synthesize`, or `merge`.
  `session_id` and `entry_id` name the message the call was made for,
  when there is one.
- **Which run.** `run` is `live` for work an arriving record caused, or
  the name of a backfill.
- **The tokens.** `input_tokens`, `output_tokens`, and the cache and
  reasoning counts when the provider reports them. The two providers name
  these differently, and the ledger keeps one set of names.
- **How long.** `duration_ms`, from the request to the answer.
- **How it ended.** `outcome` is `ok`, `refused`, or `error`, with
  `error_type` on the last two.

The token names match the six the record already lifts out of the
harness telemetry into `logs`, so one name means one thing across the two
sources.

## The two providers

| Provider | Model | Tokens | Request id |
|---|---|---|---|
| TypeSafe, the System One model | the versioned `jev-...` from the answer | `usage.input_tokens`, `usage.output_tokens` | the `x-typesafe-request-id` header |
| DeepInfra | the model in the answer | `usage.prompt_tokens`, `usage.completion_tokens` | the answer's `id` |

The local embedding model calls no API and reports no tokens, so it is
not in the ledger.

## The run

The question behind this plan is the deep backfill. A total alone cannot
separate it from ongoing use, so a call records the run it belongs to.

A record that arrives schedules its work as `live`. A range read
schedules its work under a name the caller gives, and the name travels
with the scheduled task. A call made by that task carries the name, and
so does a call made by the writing the classification scheduled after it.
The deep backfill is one name, and its calls total under that name.

## Reading it

`/usage` totals the ledger by day, by model, by task, and by run. A raw
read returns the calls themselves, newest first, narrowed by provider,
model, task, run, session, outcome, or a time range, with a cursor that
continues where the page ended. The cursor is the last row, so a page
does not repeat a row while new calls arrive. An agent reads the calls
rather than a total.

## What it does not do

- **No dollars.** The ledger records tokens and the exact model. A price
  is a later plan, because a price changes and a token count does not.
  The row keeps the model, so a later price can be applied to the calls
  already recorded.
- **No harness calls.** The harness's own calls stay in `logs`.
- **No change to the calls.** The ledger records what a task would have
  done anyway.
- **No counting of the SDK's own retries.** The ledger records one row
  per call through a client. The TypeSafe SDK retries some failures
  inside the call, and those attempts are not visible here.

## How it is proved

- A classify call writes a row with the versioned model, the task, and
  the input and output tokens.
- A synthesize call writes a row with the DeepInfra model and the prompt
  and completion tokens.
- A merge call writes a row with the versioned model.
- A provider refusal writes a row with the outcome and whatever the
  provider reported, and the task still returns.
- A task that retries writes one row per attempt, because each attempt is
  a call.
- A call made under a named backfill carries the name, and a call made
  from an arriving record carries `live`.
- A call whose ledger write fails is logged, and the call proceeds.
- `/usage` totals calls and tokens by day, by model, by task, and by run.
- A raw read returns the calls newest first, narrowed by any field, and a
  cursor continues without repeating a row.
- The local embedder writes no row.

## Open questions

- **Whether this and the harness's numbers become one read.** The
  harness's tokens are in `logs`, and the service's are in `model_calls`.
  One view over both answers what the store costs to run, and the two
  sources are not the same.
- **What a run is named.** A name given by the caller says what the
  backfill was. A name the service derives from the request says less and
  asks nothing.
- **Whether the embedder's time belongs here.** It calls no API and
  spends no tokens, and an embed over the table takes minutes. The time
  is real, and the ledger has no place for it.

## What the drill measured

The ledger ran against the compose stack over one writer backfill, named
`drill`. 401 calls were recorded. 376 went to DeepSeek-V4.1-Flash for the
writer, 1,274,994 input tokens and 12,433 output. 25 went to Jev for the
merger, 12,777 input and 500 output. Every call ended `ok`.

`/usage` returned those numbers four ways. By day, one day holds all of
them. By model, the two models above. By task, `synthesize` and `merge`.
By run, every call carries `drill`, because the name travelled from the
`/write` request into each scheduled task and into the calls those tasks
made. A record that arrives carries `live` instead, which is the default
and the path the app takes from `/v1/logs`.

The run name is what separates a deep backfill from ongoing use, and the
drill shows the separation holds end to end. A raw read returned the calls
newest first and continued from the cursor through a page without
repeating a row.
