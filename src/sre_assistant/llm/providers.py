"""Provider abstraction: the rest of the codebase only sees LangChain's
`BaseChatModel` / `Embeddings` interfaces, never a vendor SDK directly.

Swapping local Ollama for Azure OpenAI is therefore a config change, not a code change
(Strategy pattern / dependency inversion).
"""

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from sre_assistant.config import Settings, get_settings
from sre_assistant.llm.embeddings import PrefixedEmbeddings


def get_chat_model(settings: Settings | None = None) -> BaseChatModel:
    s = settings or get_settings()
    match s.llm_provider:
        case "ollama":
            from langchain_ollama import ChatOllama

            return ChatOllama(model=s.chat_model, base_url=s.ollama_base_url, temperature=s.temperature)
        case "azure_openai":
            from langchain_openai import AzureChatOpenAI  # installed in Phase 7

            return AzureChatOpenAI(
                azure_deployment=s.chat_model,
                azure_endpoint=s.azure_openai_endpoint,
                api_version=s.azure_openai_api_version,
                api_key=s.azure_openai_api_key,
                temperature=s.temperature,
            )
    raise ValueError(f"Unsupported provider: {s.llm_provider}")


def get_embeddings(settings: Settings | None = None) -> Embeddings:
    """Embeddings wrapped with the model's query/document instruction prefixes (if any)."""
    s = settings or get_settings()
    match s.llm_provider:
        case "ollama":
            from langchain_ollama import OllamaEmbeddings

            inner = OllamaEmbeddings(model=s.embedding_model, base_url=s.ollama_base_url)
        case "azure_openai":
            from langchain_openai import AzureOpenAIEmbeddings

            inner = AzureOpenAIEmbeddings(
                azure_deployment=s.embedding_model,
                azure_endpoint=s.azure_openai_endpoint,
                api_version=s.azure_openai_api_version,
                api_key=s.azure_openai_api_key,
            )
        case _:
            raise ValueError(f"Unsupported provider: {s.llm_provider}")
    return PrefixedEmbeddings(inner, s.embedding_model)
