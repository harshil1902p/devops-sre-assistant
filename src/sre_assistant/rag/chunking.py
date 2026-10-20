"""Chunking strategies.

parent_child (structure-aware, the one we use):
  - CHILD  = a small block inside a `##` section: a whole code fence, a list item, or a paragraph, packed up
             to `child_max_tokens`. This is what we embed and keyword-index, so matches are precise.
  - PARENT = what the LLM reads. Queries usually match *symptoms / error signatures*, but the answer lives in
             *Root Cause / Permanent Fix*, so the parent must span both: the WHOLE document when it fits in
             `parent_max_tokens`, otherwise the individual `##` section (long post-mortems).
  - Code fences are atomic: never split a stack trace or YAML block in half.
  - Every child is prefixed with a context header "<doc title> | <section>" so that a lone code block
    still "knows" which runbook and section it belongs to (contextual chunk headers).

naive (baseline for comparison):
  - fixed token windows with overlap over the raw body, ignoring structure. Parent == child.
"""

import re
from dataclasses import dataclass

from sre_assistant.corpus.schema import Document
from sre_assistant.rag.tokens import count_tokens, split_tokens


@dataclass(frozen=True)
class Parent:
    parent_id: str
    doc_id: str
    section: str
    content: str
    token_count: int


@dataclass(frozen=True)
class Child:
    chunk_id: str
    parent_id: str
    doc_id: str
    section: str
    index: int
    content: str
    token_count: int


@dataclass(frozen=True)
class Chunked:
    parents: list[Parent]
    children: list[Child]


_FENCE = re.compile(r"^\s*```")
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown on `## ` headers (ignoring headers inside code fences). Preamble -> 'Overview'."""
    sections: list[tuple[str, list[str]]] = [("Overview", [])]
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    return [(name, "\n".join(lines).strip()) for name, lines in sections if "\n".join(lines).strip()]


def split_blocks(text: str) -> list[str]:
    """Atomic blocks: code fences, single list items, or paragraphs."""
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False

    def flush():
        if buf and "\n".join(buf).strip():
            blocks.append("\n".join(buf).strip())
        buf.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            if not in_fence:
                flush()
                in_fence = True
                buf.append(line)
            else:
                buf.append(line)
                in_fence = False
                flush()
            continue
        if in_fence:
            buf.append(line)
        elif not line.strip():
            flush()
        elif _LIST_ITEM.match(line):
            flush()
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return blocks


def _pack(blocks: list[str], max_tokens: int) -> list[str]:
    """Greedily merge consecutive small blocks up to max_tokens; oversized prose is split by lines."""
    packed: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for block in blocks:
        n = count_tokens(block)
        if n > max_tokens and not block.lstrip().startswith("```"):
            pieces = [ln for ln in block.splitlines() if ln.strip()]
            if len(pieces) > 1:
                packed.extend(_pack(pieces, max_tokens))
                continue
        if cur and cur_tokens + n > max_tokens:
            packed.append("\n\n".join(cur))
            cur, cur_tokens = [], 0
        cur.append(block)
        cur_tokens += n
    if cur:
        packed.append("\n\n".join(cur))
    return packed


def chunk_parent_child(doc: Document, child_max_tokens: int = 128, parent_max_tokens: int = 1500) -> Chunked:
    title = doc.metadata.title
    sections = split_sections(doc.body)
    whole_doc = count_tokens(doc.body) <= parent_max_tokens
    parents: list[Parent] = []
    children: list[Child] = []

    if whole_doc:
        content = f"[{doc.doc_id}] {title}\n{doc.body}"
        parents.append(Parent(doc.doc_id, doc.doc_id, "Full document", content, count_tokens(content)))

    for section, text in sections:
        slug = _slug(section)
        parent_id = doc.doc_id if whole_doc else f"{doc.doc_id}#{slug}"
        if not whole_doc:
            content = f"[{doc.doc_id}] {title}\n## {section}\n{text}"
            parents.append(Parent(parent_id, doc.doc_id, section, content, count_tokens(content)))
        for i, piece in enumerate(_pack(split_blocks(text), child_max_tokens)):
            content = f"{title} | {section}\n{piece}"
            children.append(
                Child(
                    f"{doc.doc_id}#{slug}/{i}",
                    parent_id,
                    doc.doc_id,
                    section,
                    i,
                    content,
                    count_tokens(content),
                )
            )
    return Chunked(parents, children)


def chunk_naive(doc: Document, size: int = 256, overlap: int = 32) -> Chunked:
    parents: list[Parent] = []
    children: list[Child] = []
    for i, piece in enumerate(split_tokens(doc.body, size, overlap)):
        pid = f"{doc.doc_id}#w{i}"
        n = count_tokens(piece)
        parents.append(Parent(pid, doc.doc_id, f"window {i}", piece, n))
        children.append(Child(f"{pid}/0", pid, doc.doc_id, f"window {i}", 0, piece, n))
    return Chunked(parents, children)
