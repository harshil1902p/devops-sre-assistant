"""Rule-based secret redaction applied to EVERYTHING the MCP server returns (logs, env, config, probes).

Phase 3 baseline: deterministic regexes for well-known secret shapes. Phase 5 adds NER (spaCy) for
unstructured PII and a measured leakage test-suite. Redaction happens at the tool boundary: secrets
must never reach the LLM provider, the agent's memory, or traces.
"""

import re
from dataclasses import dataclass

MASK = "***REDACTED***"

# A key "looks secret" if its last word is one of these (so `jwt_signing_key` matches, `token_count` doesn't).
_SECRET_KEY = re.compile(
    r"(?:^|[_\-.])(password|passwd|pwd|secret|token|apikey|api_key|key|credentials?|connectionstring|sas)$",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # scheme://user:password@host  ->  scheme://user:***@host
    ("url_credentials", re.compile(r"(?P<pre>\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)(?P<secret>[^@\s/]+)(?P<post>@)", re.I),
     rf"\g<pre>{MASK}\g<post>"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), MASK),
    ("bearer", re.compile(r"(?P<pre>\bBearer\s+)[A-Za-z0-9._\-~+/]{12,}=*", re.I), rf"\g<pre>{MASK}"),
    ("azure_key", re.compile(r"(?P<pre>\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)=)[^;\s\"']+", re.I),
     rf"\g<pre>{MASK}"),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    # key: value / key=value / "key": "value" where the key name looks secret
    ("secret_assignment", re.compile(
        r"(?P<pre>[\"']?(?P<key>[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|signing[_\-]?key"
        r"|private[_\-]?key|credentials?))[\"']?\s*[:=]\s*[\"']?)(?P<secret>[^\s\"',;}]+)", re.I),
     rf"\g<pre>{MASK}"),
]  # fmt: skip


@dataclass(frozen=True)
class Redacted:
    text: str
    count: int
    kinds: dict[str, int]


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key.strip()))


def redact_text(text: str) -> Redacted:
    kinds: dict[str, int] = {}
    for name, pattern, repl in _PATTERNS:
        text, n = pattern.subn(repl, text)
        if n:
            kinds[name] = kinds.get(name, 0) + n
    return Redacted(text, sum(kinds.values()), kinds)


def redact(text: str) -> str:
    return redact_text(text).text


def redact_mapping(data, _count: list[int] | None = None):
    """Recursively redact values of secret-looking keys in parsed YAML/JSON, plus string patterns everywhere."""
    counter = _count if _count is not None else [0]
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(k, str) and is_secret_key(k) and v not in (None, ""):
                out[k] = MASK
                counter[0] += 1
            else:
                out[k] = redact_mapping(v, counter)
        return out
    if isinstance(data, list):
        return [redact_mapping(v, counter) for v in data]
    if isinstance(data, str):
        r = redact_text(data)
        counter[0] += r.count
        return r.text
    return data
