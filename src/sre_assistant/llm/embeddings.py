"""Embedding helpers.

Some embedding models are *asymmetric*: they were trained with different instruction prefixes for
queries and documents. nomic-embed-text expects "search_query: " / "search_document: ".
Forgetting them silently degrades retrieval. OpenAI text-embedding-3 models need no prefix.
"""

from langchain_core.embeddings import Embeddings

KNOWN_PREFIXES: dict[str, tuple[str, str]] = {
    # model name prefix: (query_prefix, document_prefix)
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "bge-": ("Represent this sentence for searching relevant passages: ", ""),
}


class PrefixedEmbeddings(Embeddings):
    def __init__(self, inner: Embeddings, model_name: str, batch_size: int = 32):
        self.inner = inner
        self.batch_size = batch_size
        self.query_prefix, self.doc_prefix = next(
            (p for name, p in KNOWN_PREFIXES.items() if model_name.startswith(name)), ("", "")
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [self.doc_prefix + t for t in texts[i : i + self.batch_size]]
            out.extend(self.inner.embed_documents(batch))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self.query_prefix + text)
