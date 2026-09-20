"""Configuration the service reads from its environment."""

from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
