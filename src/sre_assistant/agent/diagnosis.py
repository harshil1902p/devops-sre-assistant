"""Structured final answer + grounding checks.

The model must answer in this schema (enforced with JSON-schema structured output). We then VERIFY
citations against what search_runbooks actually returned in this conversation: a citation the
retriever never produced is a hallucination and is removed and reported.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Diagnosis(BaseModel):
    service: str
    status: Literal["down", "degraded", "recovered", "healthy", "unknown"]
    summary: str = Field(description="2-3 sentences: what is happening and why")
    evidence: list[str] = Field(description="facts quoted from tool outputs, each with its source tool and time")
    root_cause: str
    confidence: Literal["low", "medium", "high"]
    immediate_mitigation: list[str] = Field(description="safe steps to restore service now")
    permanent_fix: list[str] = Field(description="changes that prevent recurrence (code/config/alerts)")
    citations: list[str] = Field(description="runbook citations exactly as returned by search_runbooks")
    separate_issues: list[str] = Field(
        description="other, independent problems seen in the window (with time and evidence); empty if none"
    )
    open_questions: list[str] = Field(description="what is still unknown or unverified")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_control_chars(cls, v):
        def clean(s):
            return _CTRL.sub("", s) if isinstance(s, str) else s

        return [clean(x) for x in v] if isinstance(v, list) else clean(v)


class GroundingReport(BaseModel):
    valid_citations: list[str]
    invalid_citations: list[str]
    warnings: list[str]


_DOC_ID = re.compile(r"\b(?:RB|PM|KB)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def check_grounding(d: Diagnosis, retrieved_doc_ids: set[str], tools_used: set[str]) -> GroundingReport:
    valid, invalid, warnings = [], [], []
    for c in d.citations:
        ids = _DOC_ID.findall(c)
        (valid if ids and all(i in retrieved_doc_ids for i in ids) else invalid).append(c)
    if invalid:
        warnings.append(f"removed {len(invalid)} citation(s) not returned by search_runbooks: {invalid}")
    if retrieved_doc_ids and not valid:
        warnings.append("runbooks were retrieved but none were cited")
    if "search_runbooks" not in tools_used:
        warnings.append("no runbook search was performed; answer is not grounded in institutional knowledge")
    if not d.evidence:
        warnings.append("no evidence quoted from live tools")
    return GroundingReport(valid_citations=valid, invalid_citations=invalid, warnings=warnings)


def render_markdown(d: Diagnosis, g: GroundingReport) -> str:
    def bullets(items):
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    parts = [
        f"## Diagnosis: `{d.service}` is **{d.status}** (confidence: {d.confidence})",
        d.summary,
        "### Evidence",
        bullets(d.evidence),
        "### Root cause",
        d.root_cause,
        "### Immediate mitigation",
        bullets(d.immediate_mitigation),
        "### Permanent fix",
        bullets(d.permanent_fix),
        "### Sources",
        bullets(g.valid_citations),
    ]
    if d.separate_issues:
        parts += ["### Separate issues in the same window", bullets(d.separate_issues)]
    if d.open_questions:
        parts += ["### Open questions", bullets(d.open_questions)]
    if g.warnings:
        parts += ["### ⚠️ Grounding warnings", bullets(g.warnings)]
    return "\n\n".join(parts)
