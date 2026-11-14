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
        repr=False,    # it holds the database password
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

    # Which model writes the answers (app/rag/llm.py):
    #   ollama  the local model on this computer: laptop and facilities
    #   openai  any OpenAI-compatible API, e.g. Together AI for the online
    #           trial. Set GENERATION_API_KEY, and GENERATION_MODEL to the
    #           provider's name for the model (Together AI: zai-org/GLM-5.3-Flash;
    #           it no longer offers Llama pay-per-use).
    # Search always stays local (bge-m3 on Ollama), whichever is chosen.
    generation_provider: str = Field(default="ollama")
    generation_api_base: str = Field(default="https://api.together.xyz/v1")
    # repr=False on every secret: settings must never appear in an error
    # message or log with a secret in them (a failed test once printed part
    # of the server's login secret into a public GitHub log).
    generation_api_key: str = Field(default="", repr=False)

    # Logins. True (the default): everyone logs in, as on the online trial,
    # which anyone with the address can reach. False: a facility installation;
    # normal users use the assistant without logging in, and only super users
    # log in, at /account/login. Only safe while the computer cannot be
    # reached from the internet.
    login_required: bool = Field(default=True)

    # Online trial only: a note at the top of the greeting saying the site is
    # a proof of concept and the assistant will be installed at facilities
    # (app/ui/welcome.py).
    trial_notice: bool = Field(default=False)

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

    # User roles (stakeholder item 5). Ordinary users get no answers from these
    # manuals: searches skip them, and a question whose best match is in one of
    # them gets a "limited to super users" reply (app/rag/access.py). Super
    # users see everything and manage accounts at /admin.
    # Env RESTRICTED_MODULES: manual titles, comma-separated.
    restricted_modules: str = Field(
        default="Human Resources Management Module,User and Facility Management Module"
    )
    # A question counts as being about a restricted task only if its best
    # restricted match beats the best open match by at least this much
    # (bge-m3 cosine). Below that, "leave" or "reset my password" is answered
    # from the open manuals. Calibrated in RAG_modifications.md (M12).
    restricted_gap: float = Field(default=0.03)

    # Chainlit. CHAINLIT_AUTH_SECRET signs the login cookie; it is required
    # once the login page is on. Generate one with: chainlit create-secret
    chainlit_auth_secret: str = Field(default="", repr=False)

    # App
    app_log_level: str = Field(default="INFO")
    data_dir: Path = Field(default=Path("/data"))
    images_url_prefix: str = Field(default="/images")

    @property
    def restricted_manuals(self) -> list[str]:
        return [m.strip() for m in self.restricted_modules.split(",") if m.strip()]

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
