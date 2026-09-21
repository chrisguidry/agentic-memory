"""Configuration the service reads from its environment."""

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

    # What a turn is handed. A session's first prompt gets the top of its
    # scope's list, and every prompt after it gets only the statements that are
    # about what was typed, or nothing.
    recall_session_limit: int = 10
    recall_prompt_limit: int = 5

    # How far above the ninety-ninth percentile of the scope's similarities a
    # statement has to score to be handed over. Measured against one scope of
    # 379 statements, the best match was 0.087 and 0.099 above the baseline on
    # two real hits and 0.061 to 0.072 on three prompts about nothing in the
    # record, so 0.08 is between them and the room is thin. It is a guess until
    # the handouts in `injections` are labelled.
    recall_margin: float = 0.08


@lru_cache
def get_settings() -> Settings:
    return Settings()
