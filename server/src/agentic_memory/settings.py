"""Configuration the service reads from its environment."""

from datetime import timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

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

    # The person's time zone, as an IANA name such as America/New_York. The
    # writer is told when a message was said in this zone, so "tomorrow
    # morning" and "Monday" mean the person's morning and Monday, and not
    # UTC's. The record carries no zone for the person, so this is UTC until it
    # is set, and a name that is not a zone is refused at startup.
    time_zone: ZoneInfo = ZoneInfo("UTC")

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
    # about what was typed, or nothing. A session's first prompt can also get
    # the top of its scope's list, from the statements that have a scope. The
    # list is the same few statements for every session in a scope, whatever
    # it is about, and over 90 probe sessions it was relevant about 2 times in
    # 70, so it is off unless a deployment asks for it.
    recall_opening_limit: int = Field(0, ge=0)
    recall_prompt_limit: int = 5

    # How far above the ninety-ninth percentile of the scope's similarities a
    # statement has to score to be handed over. It was measured over 94 probe
    # prompts against a store of about 2,900 live statements. Each statement
    # a prompt was handed, or should have been, was labelled good, noisy, or
    # wrong. A missed good statement costs as much as a noisy one, and a wrong
    # one costs twice as much. A margin of 0.08 with an actionable threshold
    # of 0.2 handed 19 good, 18 noisy, and 4 wrong statements. A margin of
    # 0.09 with the threshold below handed 14 good, 7 noisy, and 2 wrong, and
    # 0.10 handed 11, 3, and 2 at the same cost, so the margin is the one that
    # hands more good statements. No absolute cutoff, band under the best
    # match, or baseline over the statements nearest the prompt did clearly
    # better. Most misses are not the margin's to fix: 18 of the 85 good
    # statements were not among the twenty nearest to their prompt.
    recall_margin: float = 0.09

    # How sure the System One model has to be that an agent starting new work
    # would act differently for knowing a statement, before the match hands the
    # statement out. A statement that has no answer yet is handed as before.
    # Below 0.2, the answers were remarks that some piece of work went well.
    # Over the same 94 prompts, a threshold of 0.6 in place of 0.2 took out
    # noisy statements and no good one.
    recall_actionable: float = 0.6

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

    # Sorting a prospective statement. The System One model's answers are read
    # in order, and the first one at or above its threshold says where the
    # statement goes: a commitment, then a decision, then the state of the work.
    # A commitment takes the moment the model chose only when the model's
    # confidence in it is at or above `sort_moment`.
    #
    # Over 80 statements sorted by hand into 9 commitments, 42 decisions, and
    # 29 statements of the state of the work, 0.5 on each put 59 where the
    # hand put them, ended no decision, and left no decision prospective. The
    # commitment and state thresholds moved that by one either way between 0.4
    # and 0.7. The decision threshold is the one to move with care: 15 of the
    # 29 statements of the state of the work score above it and are kept as
    # decisions, and at 0.55 two decisions end.
    sort_commitment: float = 0.5
    sort_decision: float = 0.5
    sort_state: float = 0.5
    sort_moment: float = 0.5

    # How sure the System One model has to be that the newer statement of a pair
    # in the band settles the question the older one settled, in a different
    # way, before the older one retires into it. A yes retires a statement that
    # says something the newer one does not, so this question has a threshold
    # of its own, apart from the one half the first question is read at. Over
    # one scope of 2,081 statements, 7 of 25 merges this question made at 0.5
    # were wrong, and most of the wrong ones were answered from 0.50 to 0.62.
    # At 0.6, 6 of those 7 do not merge, and 4 of the 18 right ones do not
    # either. With the question of what the newer statement keeps in force, a
    # blind judge found 9 of 24 merges this question made at 0.6 wrong, most
    # of them a statement retired when one part of it changed and the rest was
    # dropped. Read from that pass's answers, 0.7 leaves 4 wrong of the 18 it
    # keeps. It is still about one in five, so no pass over the stored
    # statements runs until this question is made stricter.
    merge_settles: float = 0.7

    # How sure the System One model has to be that the newer statement of a pair
    # keeps everything a reader needs from the older one, before a pair that
    # says the same thing merges. Over 59 merges from a pass over the whole
    # store, every wrong merge the first question made was answered below 0.4
    # here. At 0.5, two right merges answered 0.42 and 0.47 stand as well.
    merge_keeps: float = 0.4

    # Whether a statement is compared with neighbours of other kinds, and not
    # only with its own. One decision is often written once as praise and once
    # as a fact, and this is what would merge the two. It is off, because over
    # one scope of 2,081 statements, 15 of 40 merges it made were judged wrong,
    # and the first question merged pairs that are only about the same area at
    # 0.53 to 0.72, where the right merges were answered too. No threshold on
    # the first question separates the two.
    merge_across_kinds: bool = False

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
