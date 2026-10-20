"""Second-stage reranking with a cross-encoder.

Bi-encoder (embeddings): query and document are embedded SEPARATELY, so it's fast and indexable but coarse.
Cross-encoder: reads (query, document) TOGETHER with full attention, which is accurate but too slow to run
over a whole corpus. Standard pattern: cheap retrievers for recall, then a cross-encoder over the top-N
candidates for precision. Azure AI Search's "semantic ranker" plays this role in the cloud (Phase 7).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class Reranker(Protocol):
    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model_name: str, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir or str(Path.home() / ".cache" / "fastembed")
        self._model = None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        if self._model is None:  # lazy: the ONNX model loads only on first use
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(self.model_name, cache_dir=self.cache_dir)
        return [float(s) for s in self._model.rerank(query, list(texts))]
