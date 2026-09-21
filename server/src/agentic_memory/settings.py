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

    # How many exchanges of a session go into the window. A reply means
    # something next to what it replies to, so the window holds both sides.
    classify_rounds: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
