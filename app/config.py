"""Application configuration, read from environment variables.

All settings live in one place. Import `settings` (the module-level singleton)
anywhere in the codebase; do not read os.environ directly.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    # We deliberately read from APP_DATABASE_URL (not DATABASE_URL) so that
    # Chainlit's auto-data-layer detection — which scans for the env var
    # `DATABASE_URL` at chainlit-CLI startup — does NOT pick up our DSN and
    # try to use its own (incompatible) Thread/User table schema.
    database_url: str = Field(
        default="postgresql://emr:emr@db:5432/emr_helper",
        validation_alias="APP_DATABASE_URL",
        description="asyncpg-compatible Postgres DSN (env: APP_DATABASE_URL).",
    )

    # Ollama
    ollama_base_url: str = Field(default="http://ollama:11434")
    embedding_model: str = Field(default="bge-m3")
    embedding_dim: int = Field(default=1024)
    generation_model: str = Field(default="llama3.2:3b")
    reranker_model: str = Field(default="bge-reranker-v2-m3")

    # Chainlit
    chainlit_auth_secret: str = Field(default="")

    # App
    app_log_level: str = Field(default="INFO")
    data_dir: Path = Field(default=Path("/data"))
    images_url_prefix: str = Field(default="/images")

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def knowledge_base_dir(self) -> Path:
        return self.data_dir / "knowledge_base"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
