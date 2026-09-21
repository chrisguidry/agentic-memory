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


@lru_cache
def get_settings() -> Settings:
    return Settings()
