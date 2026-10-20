"""One entry point for everything that crosses the trust boundary towards an LLM.

Order matters:
  1. secrets  -> redacted       (so "postgres://u:pw@10.0.0.7" loses the password before anything else)
  2. PII      -> pseudonymised  (the IP becomes <IP:ab12cd>, consistently)
  3. injection-> neutralised    (instruction-like text in DATA is blanked out and reported)
"""

from dataclasses import dataclass, field
from functools import lru_cache

from sre_assistant.guardrails.injection import neutralise
from sre_assistant.guardrails.pii import PIIScrubber
from sre_assistant.guardrails.secrets import redact_text


@dataclass
class SanitizeReport:
    redactions: int = 0
    pii: dict[str, int] = field(default_factory=dict)
    injection_flags: list[str] = field(default_factory=list)

    def merge(self, other: "SanitizeReport") -> None:
        self.redactions += other.redactions
        for k, v in other.pii.items():
            self.pii[k] = self.pii.get(k, 0) + v
        self.injection_flags += [f for f in other.injection_flags if f not in self.injection_flags]


class Sanitizer:
    def __init__(self, scrubber: PIIScrubber | None = None):
        self.scrubber = scrubber or PIIScrubber(use_ner=False)

    def sanitize_many(self, texts: list[str | None]) -> tuple[list[str | None], SanitizeReport]:
        report = SanitizeReport()
        idx = [i for i, t in enumerate(texts) if t]
        stage1 = []
        for i in idx:
            r = redact_text(texts[i])
            report.redactions += r.count
            stage1.append(r.text)
        out = list(texts)
        for i, scrubbed in zip(idx, self.scrubber.scrub_many(stage1), strict=True):
            for k, v in scrubbed.counts.items():
                report.pii[k] = report.pii.get(k, 0) + v
            text, findings = neutralise(scrubbed.text)
            for f in findings:
                # Never quote the payload in the flag: the flag itself goes to the LLM and would re-inject it.
                flag = f"possible prompt injection ({f.kind} pattern) neutralised"
                if flag not in report.injection_flags:
                    report.injection_flags.append(flag)
            out[i] = text
        return out, report

    def sanitize(self, text: str) -> tuple[str, SanitizeReport]:
        [out], report = self.sanitize_many([text])
        return out, report


@lru_cache
def default_sanitizer(key: bytes | None = None, use_ner: bool = True) -> Sanitizer:
    return Sanitizer(PIIScrubber(key=key, use_ner=use_ner))
