"""Render the scenario catalogue (data/scenarios/*.yaml) into a Markdown knowledge base.

Each scenario becomes three documents with consistent, header-based structure:
  runbooks/RB-<code>.md       how to diagnose and fix (procedural)
  postmortems/PM-<inc>.md     what happened once (narrative, timeline, RCA)
  kb/KB-<nnnn>.md             StackOverflow-style Q&A (community knowledge)

Deterministic: the same catalogue always produces byte-identical files, so the corpus is
reviewable in git and evaluation results are reproducible.
"""

from datetime import date, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sre_assistant.corpus.schema import Environment, Infrastructure


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeExample(_Strict):
    lang: str
    code: str


class Postmortem(_Strict):
    incident_id: str = Field(pattern=r"^INC-\d{4}-\d{4}$")
    date: date
    environment: Environment
    duration_min: int = Field(gt=0)
    impact: str
    detection: str
    timeline: list[str] = Field(min_length=2)
    trigger: str
    resolution: str
    action_items: list[str] = Field(min_length=1)
    lesson: str


class KbThread(_Strict):
    title: str
    question: str
    answer: str
    comment: str


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    code: str = Field(pattern=r"^[A-Z0-9-]+$")
    title: str
    service: str
    language: str
    infrastructure: Infrastructure
    error_type: str
    severity: str = Field(pattern=r"^SEV[1-4]$")
    error_codes: list[str] = Field(min_length=1)
    tags: list[str]
    log_excerpt: str
    symptoms: list[str]
    root_cause: str
    diagnosis: list[str]
    diagnosis_commands: str
    mitigation: list[str]
    permanent_fix: list[str]
    config_example: CodeExample | None = None
    postmortem: Postmortem
    kb: KbThread

    @property
    def runbook_id(self) -> str:
        return f"RB-{self.code}"

    @property
    def postmortem_id(self) -> str:
        return f"PM-{self.postmortem.incident_id}"


def load_scenarios(scenario_dir: str | Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for f in sorted(Path(scenario_dir).glob("*.yaml")):
        scenarios.extend(Scenario(**item) for item in yaml.safe_load(f.read_text()))
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids")
    return scenarios


def _front_matter(meta: dict) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n"


def _base_meta(s: Scenario, doc_id: str, doc_type: str, title: str, env: str, created: date) -> dict:
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "title": title,
        "scenario": s.id,
        "service": s.service,
        "environment": env,
        "language": s.language,
        "infrastructure": s.infrastructure.value,
        "error_type": s.error_type,
        "error_codes": list(s.error_codes),
        "severity": s.severity,
        "created": created,
        "tags": sorted(s.tags),
    }


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1))


def _code(lang: str, code: str) -> str:
    return f"```{lang}\n{code.rstrip()}\n```"


def render_runbook(s: Scenario, kb_id: str) -> str:
    meta = _base_meta(
        s, s.runbook_id, "runbook", f"Runbook: {s.title}", "any", s.postmortem.date + timedelta(days=7)
    )
    fix_code = f"\n\n{_code(s.config_example.lang, s.config_example.code)}" if s.config_example else ""
    body = f"""# Runbook: {s.title}

> **Service:** `{s.service}` | **Severity:** {s.severity} | **Error type:** `{s.error_type}` | **Infrastructure:** {s.infrastructure.value}

## When To Use This Runbook
Use this runbook when `{s.service}` (or a similar {s.language} service) reports any of the error signatures below.

{_code("text", chr(10).join(s.error_codes))}

## Symptoms
{_bullets(s.symptoms)}

## Example Log Output
{_code("text", s.log_excerpt)}

## Root Cause
{s.root_cause.strip()}

## Diagnosis Steps
{_numbered(s.diagnosis)}

{_code("bash", s.diagnosis_commands)}

## Mitigation (Stop The Bleeding)
{_bullets(s.mitigation)}

## Permanent Fix
{_bullets(s.permanent_fix)}{fix_code}

## Related Documents
- Post-mortem: {s.postmortem_id} ({s.postmortem.date.isoformat()})
- Knowledge base: {kb_id}
"""
    return _front_matter(meta) + body


def render_postmortem(s: Scenario) -> str:
    pm = s.postmortem
    meta = _base_meta(
        s,
        s.postmortem_id,
        "postmortem",
        f"Post-mortem {pm.incident_id}: {s.title}",
        pm.environment.value,
        pm.date,
    )
    actions = "\n".join(f"| {n} | {a} | Open |" for n, a in enumerate(pm.action_items, 1))
    body = f"""# Post-mortem {pm.incident_id}: {s.title}

| Field | Value |
|---|---|
| Date | {pm.date.isoformat()} |
| Service | `{s.service}` |
| Environment | {pm.environment.value} |
| Severity | {s.severity} |
| Duration | {pm.duration_min} minutes |

## Summary
{pm.impact} Trigger: {pm.trigger}

## Detection
{pm.detection}

## Timeline (UTC)
{_bullets(pm.timeline)}

## Error Signatures Observed
{_code("text", s.log_excerpt)}

## Root Cause Analysis
**Trigger:** {pm.trigger}

{s.root_cause.strip()}

## Resolution
{pm.resolution}

## Action Items
| # | Action | Status |
|---|---|---|
{actions}

## Lessons Learned
{pm.lesson}

## References
- Runbook: {s.runbook_id}
"""
    return _front_matter(meta) + body


def render_kb(s: Scenario, kb_id: str) -> str:
    kb = s.kb
    meta = _base_meta(s, kb_id, "kb", kb.title, "any", s.postmortem.date - timedelta(days=60))
    body = f"""# {kb.title}

**Tags:** {", ".join(sorted(s.tags))}

## Question
{kb.question.strip()}

## Accepted Answer
{kb.answer.strip()}

## Comments
> {kb.comment}
"""
    return _front_matter(meta) + body


def generate_corpus(scenario_dir: str | Path, out_dir: str | Path) -> dict[str, list[str]]:
    """Write all documents and return a manifest {scenario_id: [doc_ids]} (the retrieval ground truth)."""
    out = Path(out_dir)
    for sub in ("runbooks", "postmortems", "kb"):
        (out / sub).mkdir(parents=True, exist_ok=True)
        for old in (out / sub).glob("*.md"):
            old.unlink()

    manifest: dict[str, list[str]] = {}
    for n, s in enumerate(load_scenarios(scenario_dir), 1):
        kb_id = f"KB-{n:04d}"
        (out / "runbooks" / f"{s.runbook_id}.md").write_text(render_runbook(s, kb_id))
        (out / "postmortems" / f"{s.postmortem_id}.md").write_text(render_postmortem(s))
        (out / "kb" / f"{kb_id}.md").write_text(render_kb(s, kb_id))
        manifest[s.id] = [s.runbook_id, s.postmortem_id, kb_id]
    return manifest
