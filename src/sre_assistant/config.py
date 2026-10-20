"""Centralised, typed configuration (12-factor: config comes from the environment).

Every value can be overridden with an env var prefixed `SRE_` or via a local `.env` file.
Secrets are typed as SecretStr so they never leak into logs or reprs.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SRE_", env_file=".env", extra="ignore")

    llm_provider: Literal["ollama", "azure_openai"] = "ollama"
    # Optional per-capability overrides. Embeddings must match the index (see SRE_EMBEDDING_DIM), so it is
    # common to move chat to a hosted model while keeping the existing embedding model.
    chat_provider: Literal["ollama", "azure_openai"] | None = None
    embedding_provider: Literal["ollama", "azure_openai"] | None = None
    num_ctx: int = Field(16384, ge=2048, description="Ollama context window (its default silently truncates)")
    chat_model: str = "qwen2.5:7b"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = Field(768, gt=0)
    temperature: float = Field(0.0, ge=0.0, le=2.0)

    ollama_base_url: str = "http://localhost:11434"

    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: SecretStr | None = None

    database_url: SecretStr = SecretStr(
        "postgresql://sre@localhost:5433/sre"
    )  # password comes from SRE_DATABASE_URL in .env

    # --- RAG ---
    child_max_tokens: int = Field(128, ge=32, description="max tokens of a matched child chunk")
    parent_max_tokens: int = Field(1500, ge=200, description="docs up to this size are one parent")
    naive_chunk_tokens: int = Field(256, ge=50)
    naive_chunk_overlap: int = Field(32, ge=0)
    candidate_k: int = Field(20, ge=1, description="hits fetched from EACH retriever before fusion")
    rrf_k: int = Field(60, ge=1, description="Reciprocal Rank Fusion damping constant")
    top_parents: int = Field(3, ge=1, description="parents (documents/sections) returned to the LLM")
    reranker_model: str | None = Field(
        "Xenova/ms-marco-MiniLM-L-6-v2", description="fastembed cross-encoder; empty to disable reranking"
    )
    rerank_candidates: int = Field(30, ge=1, description="fused child chunks scored by the cross-encoder")
    keyword_max_df: float = Field(0.25, gt=0, le=1, description="drop query terms in > this share of chunks")

    # --- Cost control (paid providers only) ---
    spend_limit_usd: float = Field(1.50, ge=0, description="hard cap on estimated paid LLM spend for this project")
    spend_ledger_path: str = ".audit/spend.jsonl"

    # --- MCP server ---
    docker_host: str | None = Field(None, description="Docker API URL; auto-detects DOCKER_HOST / Colima socket")
    mcp_log_max_tokens: int = Field(2500, ge=300, description="token budget for inspect_live_logs output")
    mcp_audit_log: str | None = ".audit/mcp_audit.jsonl"

    @property
    def effective_chat_provider(self) -> str:
        return self.chat_provider or self.llm_provider

    @property
    def effective_embedding_provider(self) -> str:
        return self.embedding_provider or self.llm_provider

    @model_validator(mode="after")
    def _check_azure(self) -> "Settings":
        uses_azure = "azure_openai" in (self.effective_chat_provider, self.effective_embedding_provider)
        if uses_azure and not self.azure_openai_endpoint:
            raise ValueError("SRE_AZURE_OPENAI_ENDPOINT is required when a provider is azure_openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
