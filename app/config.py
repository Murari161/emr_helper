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
    # llama3.2:3b chosen as the default via an A/B against qwen2.5:3b and
    # llama3.2:1b: it produces the cleanest numbered steps with bolded UI labels
    # and stays faithful to the source, at the same speed as qwen (~2x the 1B's
    # size but the 1B dropped numbered-list structure on complex procedures).
    # Override per deployment with env GENERATION_MODEL.
    generation_model: str = Field(default="llama3.2:3b")
    reranker_model: str = Field(default="bge-reranker-v2-m3")
    # Cross-encoder rerank pass. Enabled by default (best ranking quality).
    # Set env RERANKER_ENABLED=false to skip it — frees ~2GB RAM and ~13-16s
    # per query on CPU, at a small top-1 ranking-accuracy cost. Useful on
    # low-RAM / CPU-only deployments (e.g. an 8GB facility laptop).
    reranker_enabled: bool = Field(default=True)

    # Retrieval match strength. A question is answered from the manuals only
    # if its best match clears ONE of these; otherwise it gets a fixed
    # "not covered / off-topic" reply (see app/rag/fallback.py). Calibrated on
    # the 40-prompt test sheet: answerable questions scored >= 0.62 on the
    # vector lane or matched a title on BM25 (>= 1.0); undocumented and
    # off-topic ones mostly scored < 0.60 with no lexical hit. The few that
    # still pass reach the generation model, whose prompt has the same rules.
    match_strong_vector: float = Field(default=0.60)   # bge-m3 cosine similarity
    match_strong_bm25: float = Field(default=0.75)     # ts_rank_cd, title-weighted

    # When a retrieved procedure is only a summary of a same-titled procedure
    # in the manual it points to ("See module: X"), answer from the full one.
    # Set env PREFER_FULL_PROCEDURES=false to keep the raw ranking.
    prefer_full_procedures: bool = Field(default=True)

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
