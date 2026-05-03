"""Centralized environment-driven configuration.

Single source of truth for runtime settings. Imported by every layer that
needs environment-aware behavior (LLM client, tracing, retrieval).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = kavak-travel-assistant/  (parent of app/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: Literal["openai", "anthropic", "mock"] = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_temperature_default: float = 0.0
    llm_seed: int = 42
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None  # e.g. "claude-3-5-haiku-latest"
    embeddings_model: str = "text-embedding-3-small"

    # --- Logging ---
    log_level: str = "INFO"

    # --- Tracing ---
    trace_dir: Path = Field(default=PROJECT_ROOT / ".traces")
    trace_redact_pii: bool = True

    # --- Paths ---
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    flights_path: Path = Field(default=PROJECT_ROOT / "data" / "flights.json")
    airports_path: Path = Field(default=PROJECT_ROOT / "data" / "airports.json")
    kb_dir: Path = Field(default=PROJECT_ROOT / "data")
    prompts_dir: Path = Field(default=PROJECT_ROOT / "app" / "prompts")
    faiss_index_dir: Path = Field(default=PROJECT_ROOT / ".faiss_index")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
