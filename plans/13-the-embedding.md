# 13, The embedding

## The problem

The match hands a statement to a prompt when the statement's embedding is
near the prompt's. Opus validators judged about 150 probe prompts against
the live store, and after the thresholds were tuned, what the match handed
was good about two times in three. But 47 of 58 prompts were handed
nothing, and many of them had an exact answer in the store.

No threshold reaches those answers. Of 85 statements judged good for their
prompt, 18 were not among the 20 nearest to it, and some ranked in the
hundreds. A prompt that describes a situation, such as a new playbook, does
not land near the statement that names the rule for it, such as that
playbooks hold no roles. The embedding model is `bge-small-en-v1.5`, the
smallest of the four plan 04 measured, chosen because the models agreed on
fourteen prompts from one session.

## The shape

Measure a larger model against the labelled prompts, and switch to it if
it reaches more of the good statements without handing more noise.

Plan 04 measured `bge-base-en-v1.5` at 496 MB resident and 31 ms for one
prompt, against 275 MB and 12 ms for `bge-small`. The turn path's deadline
is 500 ms and a turn takes about 15 ms today, so the time fits. The vector
is 768 wide, against 384, so the columns change and every statement and
every reading is embedded again.

The candidates are `bge-base-en-v1.5` and any other model fastembed serves
that fits in the worker's memory limit with the embedder's bounded batches.
The measure is the one the validators used: for each labelled prompt, the
good statements handed, the noisy and wrong ones handed, and the good ones
missed, with a missed good statement costing as much as a noisy one and a
wrong one twice as much. The margin is tuned again for each model, because
similarities move with the model.

## The contracts

- The embedding columns take the new width, and the model's name stays on
  every row, so a vector from one model is never compared with another's.
- The switch re-embeds the statements and the readings with the bounded
  batches, and a turn during the switch reads only rows embedded by the
  model it uses.
- The model, its width, and the margin are settings.

## How it is proved

- On the labelled prompts, the new model reaches more of the good
  statements at no more cost, measured against `bge-small` at its tuned
  margin.
- The turn path stays inside its deadline on the live store, measured.
- The worker and the api stay inside their memory limits with the new
  model loaded, measured.
- Probe sessions after the switch, judged the way the validators judged
  them, hand at least as good a share as before.

## What was measured

The 73 labelled prompts ran against the 3,087 live statements from a local
restore, with the margin swept from 0.05 to 0.12 for each model. Cost counts
a missed good statement and a noisy one as 1, and a wrong one as 2.

| model | margin | good | noise | wrong | cost | good in the 20 nearest | memory |
|---|---|---|---|---|---|---|---|
| bge-small-en-v1.5 | 0.09 | 13 | 4 | 3 | 57 | 37 of 60 | 181 MB |
| bge-small-en-v1.5 | 0.115 | 8 | 0 | 1 | 54 | 37 of 60 | 181 MB |
| bge-base-en-v1.5 | 0.11 | 11 | 0 | 2 | 53 | 42 of 60 | 772 MB |
| snowflake-arctic-embed-m | 0.115 | 9 | 1 | 1 | 54 | 42 of 60 | 803 MB |
| jina-embeddings-v2-small-en | 0.055 | 15 | 3 | 2 | 52 | 41 of 60 | 210 MB |

- `bge-base` and `arctic` rank 5 more good statements in the 20 nearest,
  and hand no better at any margin, at about 4 times the memory.
- `jina-v2-small` is the only model that hands more good statements at a
  lower cost. The lead is small: a bootstrap over the prompts gives a 67%
  chance its cost is below `bge-small` at 0.115, and its margin was tuned on
  the same prompts.
- The labels came from what `bge-small` handed, so a statement no label
  covers counts as noise against the other models.

The switch to `jina-v2-small` is built on its own branch and not deployed.
It holds three things back:

- Jina puts similarities much higher, so the merge cutoffs move from 0.80
  and 0.95 to 0.89 and 0.97. Neither is proved.
- Re-embedding a statement clears its merge mark, so the switch compares
  every stored statement again, and no merge pass runs until plan 12's
  second question is stricter.
- The old image cannot fit the columns back to 384 wide, so a rollback
  runs `bge-small` on the new image.
