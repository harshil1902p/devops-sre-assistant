from pathlib import Path

import yaml

from sre_assistant.corpus.schema import DocMetadata, Document

_FENCE = "---"


def parse_document(text: str, path: str = "<memory>") -> Document:
    if not text.startswith(_FENCE + "\n"):
        raise ValueError(f"{path}: missing YAML front matter")
    _, header, body = text.split(_FENCE + "\n", 2)
    return Document(metadata=DocMetadata(**yaml.safe_load(header)), body=body.strip(), path=path)


def load_corpus(root: str | Path) -> list[Document]:
    root = Path(root)
    docs = [parse_document(p.read_text(), str(p.relative_to(root))) for p in sorted(root.rglob("*.md"))]
    ids = [d.doc_id for d in docs]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"duplicate doc_ids: {sorted(dupes)}")
    return docs
