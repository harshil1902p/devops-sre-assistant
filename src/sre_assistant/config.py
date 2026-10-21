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

    # --- Guardrails (Phase 5) ---
    pii_ner: bool = Field(True, description="spaCy NER for person names (regex detectors always run)")
    pii_key: SecretStr | None = Field(
        None, description="HMAC key for stable PII pseudonyms; random per process if unset"
    )
    enable_remediation: bool = Field(False, description="expose the approval-gated restart_service tool")
    remediation_allowed_envs: list[str] = ["staging"]
    approval_secret: SecretStr | None = Field(None, description="signs human-approval tokens; never sent to the LLM")

    # --- Azure platform (Phase 7) ---
    retriever_backend: Literal["pgvector", "azure_search"] = "pgvector"
    azure_search_endpoint: str | None = None
    azure_search_index: str = "sre-runbooks"
    # The Azure index has its own embedding model, independent of the local pgvector one (768-d nomic).
    azure_search_embedding_model: str = "text-embedding-3-small"
    azure_search_embedding_dim: int = 1536
    azure_search_rerank: Literal["cross_encoder", "semantic", "none"] = Field(
        "cross_encoder", description="semantic = Azure semantic ranker (free plan: 1,000 queries/month)"
    )
    telemetry_backend: Literal["docker", "azure_monitor", "kubernetes"] = "docker"
    log_analytics_workspace_id: str | None = None
    logs_ingestion_endpoint: str | None = None
    logs_ingestion_rule_id: str | None = None
    key_vault_url: str | None = Field(None, description="if set, missing secrets are loaded from Key Vault")

    # --- Agent API (Phase 8) ---
    api_auth: Literal["entra", "disabled"] = Field("entra", description="disabled = local development only")
    entra_tenant_id: str | None = None
    api_audience: str | None = Field(None, description="Entra app id URI or client id the tokens are issued for")
    api_rate_per_minute: int = Field(6, ge=1, description="diagnoses per principal per minute (token bucket)")
    api_max_concurrent: int = Field(4, ge=1, description="agent runs in flight per replica")
    approval_four_eyes: bool = Field(True, description="the requester of an action cannot approve it")
    checkpointer: Literal["memory", "postgres"] = "memory"
    telemetry_backend_kubernetes_namespace: str | None = Field(None, description="namespace for the k8s backend")
    appinsights_connection_string: str | None = Field(None, description="enables OpenTelemetry export")

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
        if self.retriever_backend == "azure_search" and not self.azure_search_endpoint:
            raise ValueError("SRE_AZURE_SEARCH_ENDPOINT is required when SRE_RETRIEVER_BACKEND=azure_search")
        if self.telemetry_backend == "azure_monitor" and not self.log_analytics_workspace_id:
            raise ValueError("SRE_LOG_ANALYTICS_WORKSPACE_ID is required when SRE_TELEMETRY_BACKEND=azure_monitor")
        if self.api_auth == "entra" and self.entra_tenant_id and not self.api_audience:
            raise ValueError("SRE_API_AUDIENCE is required with SRE_API_AUTH=entra")
        return self


# Settings field -> Key Vault secret name (Key Vault names allow only letters, digits and dashes).
KEY_VAULT_SECRETS = {"pii_key": "sre-pii-key", "approval_secret": "sre-approval-secret"}


def load_key_vault_secrets(settings: Settings, client=None) -> Settings:
    """Fill secrets that aren't set locally from Key Vault (keyless: DefaultAzureCredential / managed identity).

    Precedence: explicit env/.env value > Key Vault > default. Values stay SecretStr, never logged.
    """
    missing = [f for f in KEY_VAULT_SECRETS if getattr(settings, f) is None]
    if not settings.key_vault_url or not missing:
        return settings
    if client is None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(
            settings.key_vault_url, DefaultAzureCredential(exclude_interactive_browser_credential=True)
        )
    from azure.core.exceptions import ResourceNotFoundError

    update = {}
    for field in missing:
        try:
            update[field] = SecretStr(client.get_secret(KEY_VAULT_SECRETS[field]).value)
        except ResourceNotFoundError:
            continue
    return settings.model_copy(update=update)


@lru_cache
def get_settings() -> Settings:
    return load_key_vault_secrets(Settings())
