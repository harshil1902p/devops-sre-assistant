import json

import pytest

from sre_assistant.mcp_server.config_files import ConfigAccessError, resolve_config_path
from sre_assistant.mcp_server.logs import fingerprint, parse_line, summarise_logs
from sre_assistant.mcp_server.redaction import MASK, is_secret_key, redact, redact_mapping


# ------------------------------------------------------------------ redaction
@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("postgresql+psycopg://auth:REMOVED-DEMO-DB-PASSWORD@auth-db:5432/auth", "REMOVED-DEMO-DB-PASSWORD"),
        ("redis://default:s3cr3tP@ss@cache:6379", "s3cr3tP"),
        ("Authorization: Bearer abcdefghijklmnop.qrstuv", "abcdefghijklmnop"),
        ("token FAKE.JWT.REMOVED", "FAKE-JWT-PAYLOAD"),
        ("DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abc123+/def==;EndpointSuffix=core", "abc123"),
        ("aws key AKIAFAKEFAKEFAKEFAKE leaked", "AKIAFAKEFAKEFAKEFAKE"),
        ('{"api_key": "fake-api-key-0002"}', "fake-api-key-0002"),
        ("jwt_signing_key: REMOVED-DEMO-JWT-KEY", "REMOVED-DEMO-JWT-KEY"),
        ("PASSWORD=fake-pw-0001", "fake-pw-0001"),
    ],
)
def test_secrets_are_redacted(text, leaked):
    out = redact(text)
    assert leaked not in out and MASK in out


def test_url_redaction_keeps_scheme_user_and_host():
    assert redact("postgresql://auth:pw@db:5432/x") == f"postgresql://auth:{MASK}@db:5432/x"


@pytest.mark.parametrize("text", ["token_count: 5", "pool_size: 20", "max_tokens: 300", "session_ttl_seconds: 3600",
                                  "GET /healthz 200", "redis://auth-cache:6379/0"])  # fmt: skip
def test_non_secrets_are_untouched(text):
    assert redact(text) == text


def test_secret_key_detection_and_nested_mapping():
    assert is_secret_key("JWT_SIGNING_KEY") and is_secret_key("db.password") and not is_secret_key("token_count")
    data = {
        "db": {"url": "postgres://u:p@h/db", "pool_size": 5},
        "security": {"api_key": "x"},
        "list": ["Bearer abcdefghijklmnopq"],
    }
    out = redact_mapping(data)
    assert out["security"]["api_key"] == MASK and out["db"]["pool_size"] == 5
    assert "p@h" not in json.dumps(out) and "abcdefghijklmnopq" not in json.dumps(out)


# ------------------------------------------------------------- config access
@pytest.mark.parametrize(
    "path", ["../../etc/passwd", "/etc/passwd", "sub/../../secret.yaml", "app.py", "x\x00.yaml", ""]
)
def test_config_path_rejected(path):
    with pytest.raises(ConfigAccessError):
        resolve_config_path("/srv/config", path)


@pytest.mark.parametrize(("path", "expected"), [("app.yaml", "/srv/config/app.yaml"),
                                                ("nested/../app.yml", "/srv/config/app.yml"),
                                                ("/srv/config/db.json", "/srv/config/db.json")])  # fmt: skip
def test_config_path_allowed(path, expected):
    assert resolve_config_path("/srv/config", path) == expected


# ---------------------------------------------------------------------- logs
def _json(level, msg, **kw):
    return json.dumps(
        {"ts": kw.pop("ts", "2025-01-01T00:00:00.000+00:00"), "level": level, "logger": "app", "msg": msg, **kw}
    )


def test_parse_json_and_plaintext_lines():
    j = parse_line(_json("ERROR", "boom", exc_type="redis.exceptions.TimeoutError", exc_message="Timeout reading"))
    assert (j.level, j.exc_type) == ("ERROR", "redis.exceptions.TimeoutError")
    p = parse_line("2025-06-03 09:14:55 UTC [3121]: FATAL:  sorry, too many clients already")
    assert p.level == "CRITICAL" and p.ts.startswith("2025-06-03")
    p = parse_line("java.lang.OutOfMemoryError: Java heap space")
    assert p.exc_type == "java.lang.OutOfMemoryError" and p.exc_message == "Java heap space"


def test_fingerprint_normalises_volatile_values_but_keeps_status_codes():
    a = parse_line(_json("ERROR", "x", exc_type="E", exc_message="timeout 5.00 from 10.0.0.1:5432 txid 99812"))
    b = parse_line(_json("ERROR", "x", exc_type="E", exc_message="timeout 7.25 from 10.9.9.9:5432 txid 12345"))
    assert fingerprint(a) == fingerprint(b) == "E: timeout <n> from <ip> txid <n>"
    assert fingerprint(parse_line(_json("INFO", "POST /login 200"))) != fingerprint(
        parse_line(_json("INFO", "POST /login 500"))
    )


def test_summarise_groups_errors_patterns_and_numeric_trends():
    lines = [_json("INFO", "runtime stats", rss_mb=v) for v in (80.0, 150.5, 230.0)]
    lines += [_json("INFO", "starting service")]
    lines += [_json("ERROR", "unhandled", exc_type="sqlalchemy.exc.TimeoutError",
                    exc_message=f"QueuePool limit of size 5 overflow 2 reached, timeout {t}", exc="Traceback\n  line\nE")
              for t in ("5.00", "5.01", "5.02")]  # fmt: skip
    r = summarise_logs("svc", lines)
    assert r.counts_by_level == {"ERROR": 3, "INFO": 4}
    [g] = r.error_groups
    assert g.count == 3 and g.search_hint.startswith("sqlalchemy.exc.TimeoutError: QueuePool limit")
    stats = next(p for p in r.patterns if p.pattern == "runtime stats")
    assert (stats.numeric["rss_mb"].min, stats.numeric["rss_mb"].max) == (80.0, 230.0)
    assert any(p.pattern == "starting service" for p in r.patterns), "rare patterns are kept"


def test_summarise_enforces_budget_and_redacts():
    lines = [_json("INFO", f"request {i} to postgres://u:fake-pw-0001@db/x " + "pad " * 30) for i in range(500)]
    r = summarise_logs("svc", lines, max_tokens=600)
    assert r.output_tokens < 900 and r.raw_tokens > 20_000
    assert "fake-pw-0001" not in r.model_dump_json() and r.redactions > 0


# -------------------------------------------------------------- event merge
def test_merge_events_dedupes_and_orders():
    from sre_assistant.mcp_server.backends.base import RawEvent
    from sre_assistant.mcp_server.backends.docker_backend import merge_events

    persisted = [
        RawEvent("2025-01-01T00:00:01+00:00", "oom"),
        RawEvent("2025-01-01T00:00:01+00:00", "die", {"exitCode": "137"}),
    ]
    live = [
        RawEvent("2025-01-01T00:00:01+00:00", "die", {"exitCode": "137"}),
        RawEvent("2025-01-01T00:00:02+00:00", "start"),
    ]
    assert [e.action for e in merge_events(persisted, live)] == ["oom", "die", "start"]
