"""Configuration the service reads from its environment."""

from datetime import timedelta
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Everything the service reads from its environment."""

    model_config = {
        "env_prefix": "AGENTIC_MEMORY_",
        "env_file": ".env",
        "extra": "ignore",
    }

    database_url: str = "postgresql://agentic_memory:agentic_memory@127.0.0.1:5433/agentic_memory"
    host: str = "127.0.0.1"
    port: int = 4318

    redis_url: str = "redis://127.0.0.1:6379/0"
    docket_name: str = "agentic-memory"

    # The key keeps its own name rather than taking the prefix, because the
    # SDK looks for TYPESAFE_API_KEY and one variable is better than two.
    typesafe_api_key: str = Field("", validation_alias="TYPESAFE_API_KEY")
    classify_model: str = "jev-latest"

    # The model that turns a message into a sentence. It is a large model on
    # purpose: writing one sentence a person would want to read again is the
    # part that needs judgment, and it runs on a fraction of the messages.
    deepinfra_api_key: str = Field("", validation_alias="DEEPINFRA_API_KEY")
    synthesize_model: str = "deepseek-ai/DeepSeek-V4.1-Flash"

    # How many exchanges of a session go into the window. A reply means
    # something next to what it replies to, so the window holds both sides.
    classify_rounds: int = 5

    # The most characters of state the classifier sends. The provider takes about
    # 32,000 tokens of state and questions together, and code runs near three
    # characters a token, so this leaves room for the questions under the limit.
    classify_budget: int = 60_000

    # The model that embeds a statement and a prompt, so the two can be
    # compared. It is small on purpose: four local models were measured and all
    # chose the same best statement, so the smallest is enough at 275 MB
    # resident and 12 ms a prompt. The cache is where its files are kept, and
    # the compose file mounts one volume there for the service and the worker.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_threads: int = 4
    embed_cache: str = ".fastembed"

    # What a turn is handed. Every prompt gets only the statements that are
    # about what was typed, or nothing. A session's first prompt also gets the
    # top of its scope's list, from the statements that have a scope, so the
    # opening carries the standing rules for where the session is.
    recall_opening_limit: int = 3
    recall_prompt_limit: int = 5

    # How far above the ninety-ninth percentile of the scope's similarities a
    # statement has to score to be handed over. Measured against one scope of
    # 379 statements, the best match was 0.087 and 0.099 above the baseline on
    # two real hits and 0.061 to 0.072 on three prompts about nothing in the
    # record, so 0.08 is between them and the room is thin. It is a guess until
    # the handouts in `injections` are labelled.
    recall_margin: float = 0.08

    # How sure the System One model has to be that an agent starting new work
    # would act differently for knowing a statement, before the match hands the
    # statement out. A statement that has no answer yet is handed as before.
    # Over 365 statements, the nine below 0.2 were all remarks that some piece
    # of work went well. Between 0.2 and 0.35 those remarks were mixed with
    # facts worth handing out, so the threshold is 0.2. It is a guess until the
    # handed pairs are labelled.
    recall_actionable: float = 0.2

    # How many of the prompts the classifier read are compared with a new
    # prompt, and the share of them that held no memory above which the prompt
    # is handed nothing. A reply with no subject of its own lands among replies
    # like it, and the classifier found no memory in those. Over a week of 488
    # prompts, a cutoff of 0.8 on twenty neighbours handed nothing to 42, which
    # were mostly greetings, replies, and questions about the state of the
    # work. A cutoff of 0.6 took 104, and among them were requests with a
    # subject of their own. Both are guesses until the handed pairs are labelled.
    #
    # The neighbourhood stays at 40 or under, because pgvector's index returns
    # at most `hnsw.ef_search` rows, 40 by default, and a neighbourhood the
    # query cannot fill decides nothing.
    recall_neighbours: int = 20
    recall_empty_share: float = 0.8

    # The most words a prompt may have and still be handed nothing for its
    # neighbours. A request for work holds no memory either, so a long request
    # has empty neighbours too, and only a short prompt is judged by them. Over
    # a week, the neighbours silenced 42 prompts. The 25 of 12 words or fewer
    # were mostly greetings, replies, and questions about the state of the work,
    # and 4 of them had a subject. Of the 17 longer ones, 10 had a subject.
    recall_silence_words: int = 12

    # Merging statements that say the same thing. The upper cutoff is where two
    # statements are one sentence with a word moved, read over the table at
    # 0.95. Between the upper and the lower cutoff an embedding cannot tell a
    # negation from its opposite, so the System One model is asked. Below the
    # lower cutoff nothing is compared. Both are read off the table and move as
    # the corpus does, and the lower one is a guess until the model's answers in
    # the band are read.
    merge_upper: float = 0.95
    merge_lower: float = 0.80

    # How often the sweep looks for work a failed model call left undone, and
    # how far back it looks. The interval is short because the query costs
    # nothing when there is nothing to find. The lookback is a month for the
    # same reason: the ledger holds about 1,500 rows in total, so scanning a
    # month of it costs about as little as scanning an hour, and a month
    # outlives the outage that motivated this and however long it takes anyone
    # to notice one.
    sweep_interval: timedelta = timedelta(minutes=5)
    sweep_lookback: timedelta = timedelta(days=30)

    # How many times a classify or synthesize call can fail, within the
    # lookback, before the sweep stops offering the entry back. A task's own
    # retries already spend 4 attempts over about a minute; an entry that has
    # failed 12 times, three times that budget, is failing for a reason a
    # retry does not fix, and sweeping it every few minutes for a month would
    # just be that same retry budget spent forever.
    sweep_attempts: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
