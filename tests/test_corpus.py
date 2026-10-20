import json
import re
from pathlib import Path

import pytest

from sre_assistant.corpus import DocType, load_corpus, parse_document
from sre_assistant.corpus.generate import generate_corpus, load_scenarios

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "data" / "scenarios"
CORPUS = ROOT / "data" / "corpus"

REQUIRED_SECTIONS = {
    DocType.RUNBOOK: ["## Symptoms", "## Root Cause", "## Diagnosis Steps", "## Permanent Fix"],
    DocType.POSTMORTEM: ["## Timeline (UTC)", "## Root Cause Analysis", "## Resolution", "## Action Items"],
    DocType.KB: ["## Question", "## Accepted Answer"],
}


@pytest.fixture(scope="module")
def docs():
    return load_corpus(CORPUS)


def test_every_scenario_produces_three_doc_types(docs):
    scenarios = load_scenarios(SCENARIOS)
    assert len(docs) == 3 * len(scenarios)
    for s in scenarios:
        types = sorted(d.metadata.doc_type for d in docs if d.metadata.scenario == s.id)
        assert types == sorted(DocType), s.id


def test_committed_corpus_is_up_to_date(tmp_path):
    """Fails if someone edited data/scenarios/*.yaml without re-running scripts/generate_corpus.py."""
    manifest = generate_corpus(SCENARIOS, tmp_path)
    for f in tmp_path.rglob("*.md"):
        committed = CORPUS / f.relative_to(tmp_path)
        assert committed.exists(), f"missing {committed}"
        assert committed.read_text() == f.read_text(), f"stale {committed}; run scripts/generate_corpus.py"
    assert json.loads((CORPUS / "manifest.json").read_text()) == manifest


def test_generation_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_corpus(SCENARIOS, a)
    generate_corpus(SCENARIOS, b)
    for f in a.rglob("*.md"):
        assert f.read_text() == (b / f.relative_to(a)).read_text()


def test_required_sections_present(docs):
    for d in docs:
        for header in REQUIRED_SECTIONS[d.metadata.doc_type]:
            assert header in d.body, f"{d.doc_id} missing '{header}'"


def test_cross_references_resolve(docs):
    ids = {d.doc_id for d in docs}
    for d in docs:
        for ref in re.findall(r"\b((?:RB|PM|KB)-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", d.body):
            assert ref in ids, f"{d.doc_id} references unknown document {ref}"


def test_runbooks_and_postmortems_contain_an_exact_error_signature(docs):
    """Keyword (BM25) retrieval relies on exact error strings being present in the text."""
    for d in docs:
        if d.metadata.doc_type is DocType.KB:
            continue
        assert any(code in d.body for code in d.metadata.error_codes), d.doc_id


def test_parse_rejects_missing_front_matter():
    with pytest.raises(ValueError, match="front matter"):
        parse_document("# no header\n" + "x" * 300)


def test_metadata_rejects_unknown_fields():
    text = (CORPUS / "runbooks" / "RB-AUTH-DB-POOL.md").read_text()
    with pytest.raises(ValueError):
        parse_document(text.replace("severity: SEV2", "severity: SEV2\nowner: bob"))
