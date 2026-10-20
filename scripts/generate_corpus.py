"""Regenerate the knowledge base from the scenario catalogue.

uv run python scripts/generate_corpus.py
"""

import json
from pathlib import Path

from sre_assistant.corpus import load_corpus
from sre_assistant.corpus.generate import generate_corpus

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "data" / "scenarios"
CORPUS = ROOT / "data" / "corpus"


def main() -> None:
    manifest = generate_corpus(SCENARIOS, CORPUS)
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    docs = load_corpus(CORPUS)
    by_type: dict[str, int] = {}
    for d in docs:
        by_type[d.metadata.doc_type.value] = by_type.get(d.metadata.doc_type.value, 0) + 1
    words = sum(len(d.body.split()) for d in docs)
    print(f"{len(manifest)} scenarios -> {len(docs)} documents {by_type}, ~{words:,} words")


if __name__ == "__main__":
    main()
