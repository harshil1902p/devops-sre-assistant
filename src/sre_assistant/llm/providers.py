"""Provider abstraction: the rest of the codebase only sees LangChain's
`BaseChatModel` / `Embeddings` interfaces, never a vendor SDK directly.

Swapping local Ollama for Azure OpenAI is therefore a config change, not a code change
(Strategy pattern / dependency inversion). Chat and embeddings can use different providers.
"""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from sre_assistant.config import Settings, get_settings
from sre_assistant.llm.embeddings import PrefixedEmbeddings
from sre_assistant.llm.spend import SpendGuard, SpendLedger

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@lru_cache
def _entra_token_provider():
    """Keyless auth: Entra ID tokens from `az login` locally, Managed Identity in Azure (DefaultAzureCredential)."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(
        DefaultAzureCredential(exclude_interactive_browser_credential=True), COGNITIVE_SCOPE
    )


def _azure_auth(s: Settings) -> dict:
    if s.azure_openai_api_key:
        return {"api_key": s.azure_openai_api_key}
    return {"azure_ad_token_provider": _entra_token_provider()}


def get_ledger(settings: Settings | None = None) -> SpendLedger:
    s = settings or get_settings()
    return SpendLedger(s.spend_ledger_path, s.spend_limit_usd)


def get_chat_model(settings: Settings | None = None, tag: str = "") -> BaseChatModel:
    s = settings or get_settings()
    match s.effective_chat_provider:
        case "ollama":
            from langchain_ollama import ChatOllama

            return ChatOllama(
                model=s.chat_model, base_url=s.ollama_base_url, temperature=s.temperature, num_ctx=s.num_ctx
            )
        case "azure_openai":
            from langchain_openai import AzureChatOpenAI

            return AzureChatOpenAI(
                azure_deployment=s.chat_model,
                azure_endpoint=s.azure_openai_endpoint,
                api_version=s.azure_openai_api_version,
                temperature=s.temperature,
                max_retries=5,  # honours Retry-After on 429s
                callbacks=[SpendGuard(get_ledger(s), s.chat_model, tag)],  # hard cost cap on every paid call
                **_azure_auth(s),
            )
    raise ValueError(f"Unsupported chat provider: {s.effective_chat_provider}")


def get_embeddings(settings: Settings | None = None) -> Embeddings:
    """Embeddings wrapped with the model's query/document instruction prefixes (if any)."""
    s = settings or get_settings()
    match s.effective_embedding_provider:
        case "ollama":
            from langchain_ollama import OllamaEmbeddings

            inner = OllamaEmbeddings(model=s.embedding_model, base_url=s.ollama_base_url)
        case "azure_openai":
            from langchain_openai import AzureOpenAIEmbeddings

            inner = AzureOpenAIEmbeddings(
                azure_deployment=s.embedding_model,
                azure_endpoint=s.azure_openai_endpoint,
                api_version=s.azure_openai_api_version,
                **_azure_auth(s),
            )
        case _:
            raise ValueError(f"Unsupported embedding provider: {s.effective_embedding_provider}")
    return PrefixedEmbeddings(inner, s.embedding_model)
