from datetime import date

from sre_assistant.corpus.schema import DocMetadata, Document
from sre_assistant.rag.chunking import chunk_naive, chunk_parent_child, split_blocks, split_sections
from sre_assistant.rag.tokens import count_tokens, split_tokens

BODY = """# Runbook: Pool exhaustion
> intro line

## Symptoms
- Requests fail after 5 s.
- Pool shows 7 checked out.

## Example Log Output
```text
## not a header, inside a fence
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 2 reached
```

## Permanent Fix
Raise pool_size to 20 and always close sessions. This paragraph explains the connection budget in detail.
"""


def make_doc(body: str = BODY) -> Document:
    meta = DocMetadata(
        doc_id="RB-TEST-POOL", doc_type="runbook", title="Runbook: Pool exhaustion", scenario="db-pool-exhaustion",
        service="auth-service", environment="any", language="python", infrastructure="kubernetes",
        error_type="TimeoutError", error_codes=["QueuePool limit"], severity="SEV2", created=date(2025, 1, 1),
    )  # fmt: skip
    return Document(metadata=meta, body=body, path="x.md")


def test_split_sections_ignores_headers_inside_code_fences():
    names = [n for n, _ in split_sections(BODY)]
    assert names == ["Overview", "Symptoms", "Example Log Output", "Permanent Fix"]


def test_code_fences_are_atomic_and_list_items_are_separate_blocks():
    blocks = split_blocks(dict(split_sections(BODY))["Example Log Output"])
    assert len(blocks) == 1 and blocks[0].startswith("```") and blocks[0].endswith("```")
    assert split_blocks("- a\n- b\n\npara") == ["- a", "- b", "para"]


def test_small_document_is_one_parent_with_many_children():
    out = chunk_parent_child(make_doc(), child_max_tokens=32, parent_max_tokens=1500)
    assert [p.parent_id for p in out.parents] == ["RB-TEST-POOL"]
    assert len(out.children) >= 4
    assert {c.parent_id for c in out.children} == {"RB-TEST-POOL"}
    assert len({c.chunk_id for c in out.children}) == len(out.children)


def test_large_document_falls_back_to_section_parents():
    out = chunk_parent_child(make_doc(), child_max_tokens=32, parent_max_tokens=40)
    assert [p.section for p in out.parents] == ["Overview", "Symptoms", "Example Log Output", "Permanent Fix"]
    assert {c.parent_id for c in out.children} <= {p.parent_id for p in out.parents}


def test_children_carry_context_header_and_respect_budget():
    out = chunk_parent_child(make_doc(), child_max_tokens=32)
    for c in out.children:
        assert c.content.startswith(f"Runbook: Pool exhaustion | {c.section}\n")
        body = c.content.split("\n", 1)[1]
        assert count_tokens(body) <= 32 or body.startswith("```")


def test_chunking_is_deterministic():
    assert chunk_parent_child(make_doc()) == chunk_parent_child(make_doc())


def test_naive_windows_overlap_and_cover_text():
    text = " ".join(f"word{i}" for i in range(400))
    pieces = split_tokens(text, size=100, overlap=20)
    assert len(pieces) > 3
    assert pieces[0].split()[-1] in pieces[1]
    out = chunk_naive(make_doc(BODY * 5), size=64, overlap=8)
    assert len(out.parents) == len(out.children) > 1
