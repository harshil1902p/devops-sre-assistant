import pytest
from pydantic import ValidationError

from sre_assistant.config import Settings
from sre_assistant.llm.providers import get_chat_model, get_embeddings


def test_defaults_are_local_and_deterministic(monkeypatch):
    monkeypatch.delenv("SRE_LLM_PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.temperature == 0.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("SRE_CHAT_MODEL", "llama3.1:8b")
    assert Settings(_env_file=None).chat_model == "llama3.1:8b"


def test_azure_requires_endpoint():
    with pytest.raises(ValidationError, match="AZURE_OPENAI_ENDPOINT"):
        Settings(_env_file=None, llm_provider="azure_openai")


def test_secret_not_leaked_in_repr():
    s = Settings(_env_file=None, azure_openai_api_key="super-secret")
    assert "super-secret" not in repr(s)


def test_provider_factory_returns_ollama_clients():
    s = Settings(_env_file=None)
    assert type(get_chat_model(s)).__name__ == "ChatOllama"
    emb = get_embeddings(s)
    assert type(emb.inner).__name__ == "OllamaEmbeddings"
    assert emb.query_prefix == "search_query: " and emb.doc_prefix == "search_document: "
