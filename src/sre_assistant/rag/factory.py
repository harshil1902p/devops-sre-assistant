from sre_assistant.config import Settings, get_settings
from sre_assistant.llm import get_embeddings
from sre_assistant.rag.rerank import CrossEncoderReranker
from sre_assistant.rag.retriever import HybridRetriever
from sre_assistant.rag.store import VectorStore


def build_store(settings: Settings | None = None) -> VectorStore:
    s = settings or get_settings()
    return VectorStore(s.database_url.get_secret_value(), s.embedding_dim)


def build_retriever(settings: Settings | None = None, collection: str = "parent_child") -> HybridRetriever:
    s = settings or get_settings()
    reranker = CrossEncoderReranker(s.reranker_model) if s.reranker_model else None
    return HybridRetriever(build_store(s), get_embeddings(s), s, collection, reranker)
