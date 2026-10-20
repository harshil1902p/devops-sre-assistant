"""PII detection and PSEUDONYMISATION for anything that leaves the trust boundary (LLM prompts, traces).

Redaction vs pseudonymisation:
  - Secrets (passwords, keys, tokens) are REDACTED: replaced with a mask, nothing about them is useful.
  - Personal data (emails, names, IPs, phones) is PSEUDONYMISED: replaced with a stable keyed token such as
    <EMAIL:3fa9c1>. The same value always gives the same token within a key, so the model can still reason
    ("all failures come from the same client <IP:7c2e10>") without ever seeing the real value.
  - Financial identifiers (card numbers, Canadian SIN) are validated with the Luhn checksum to avoid masking
    every 16-digit number (order ids, trace ids).

Detectors: deterministic regexes first (high precision, explainable), then spaCy NER for PERSON names in
free text (regexes can't recognise names). NER is optional and lazy-loaded.
"""

import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
# Loose candidate (hex + at least two colons); ipaddress.IPv6Address decides. Handles "::" compression and
# naturally rejects timestamps (12:34:56) and MAC addresses.
_IPV6 = re.compile(r"(?<![\w:])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)[0-9A-Fa-f:]{3,39}(?![\w:])")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_SIN = re.compile(r"(?<!\d)\d{3}[ \-]\d{3}[ \-]\d{3}(?!\d)")
_KEEP_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255"}


def luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 9:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


@dataclass
class Scrubbed:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class PIIScrubber:
    def __init__(self, key: bytes | None = None, use_ner: bool = True):
        # A per-deployment secret key makes tokens unguessable (no dictionary attack on hashed emails).
        self.key = key or secrets.token_bytes(32)
        self.use_ner = use_ner

    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self.key, value.strip().lower().encode(), hashlib.sha256).hexdigest()[:6]
        return f"<{kind}:{digest}>"

    @cached_property
    def _nlp(self):
        import spacy

        return spacy.load("en_core_web_sm", disable=["parser", "lemmatizer", "tagger", "attribute_ruler"])

    def _regex_pass(self, text: str, counts: dict[str, int]) -> str:
        def sub(kind: str, pattern: re.Pattern, s: str, valid=lambda v: True) -> str:
            def repl(m: re.Match) -> str:
                v = m.group(0)
                if not valid(v):
                    return v
                counts[kind] = counts.get(kind, 0) + 1
                return self.token(kind, v)

            return pattern.sub(repl, s)

        text = sub("EMAIL", _EMAIL, text)
        text = sub("CARD", _CARD, text, lambda v: 13 <= sum(c.isdigit() for c in v) <= 19 and luhn_ok(v))
        text = sub("SIN", _SIN, text, luhn_ok)
        text = sub("PHONE", _PHONE, text)
        text = sub("IP", _IPV4, text, lambda v: v not in _KEEP_IPS)

        def ipv6_ok(v: str) -> bool:
            try:
                ip = ipaddress.IPv6Address(v)
            except ValueError:
                return False
            return not (ip.is_loopback or ip.is_unspecified)

        return sub("IP", _IPV6, text, ipv6_ok)

    def scrub_many(self, texts: Iterable[str]) -> list[Scrubbed]:
        items = list(texts)
        results = []
        for t in items:
            counts: dict[str, int] = {}
            results.append(Scrubbed(self._regex_pass(t, counts), counts))
        if self.use_ner:
            # Only lines that contain at least two capitalised words can contain a full name: skip the rest (speed).
            idx = [i for i, r in enumerate(results) if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+", r.text)]
            for i, doc in zip(idx, self._nlp.pipe([results[i].text for i in idx], batch_size=64), strict=True):
                text = results[i].text
                for ent in sorted((e for e in doc.ents if e.label_ == "PERSON"), key=lambda e: -e.start_char):
                    if len(ent.text.split()) < 2:  # single tokens are too ambiguous ("Redis", "Kafka", "Alice"?)
                        continue
                    text = text[: ent.start_char] + self.token("PERSON", ent.text) + text[ent.end_char :]
                    results[i].counts["PERSON"] = results[i].counts.get("PERSON", 0) + 1
                results[i].text = text
        return results

    def scrub(self, text: str) -> Scrubbed:
        return self.scrub_many([text])[0]
