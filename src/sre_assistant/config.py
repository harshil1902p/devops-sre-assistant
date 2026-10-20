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
    chat_model: str = "qwen2.5:7b"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = Field(768, gt=0)
    temperature: float = Field(0.0, ge=0.0, le=2.0)

    ollama_base_url: str = "http://localhost:11434"

    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _check_azure(self) -> "Settings":
        if self.llm_provider == "azure_openai" and not self.azure_openai_endpoint:
            raise ValueError("SRE_AZURE_OPENAI_ENDPOINT is required when SRE_LLM_PROVIDER=azure_openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
