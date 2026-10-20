"""Integration checks against the running demo stack (docker compose -f demo/compose.yaml up -d).

Run with:  uv run pytest -m integration
"""

import httpx
import pytest

pytestmark = pytest.mark.integration
BASE = "http://localhost:8080"


def test_readiness_checks_dependencies():
    body = httpx.get(f"{BASE}/readyz", timeout=5).json()
    assert body == {"ready": True, "checks": {"database": "ok", "redis": "ok"}}


def test_login_success_and_failure():
    ok = httpx.post(f"{BASE}/login", json={"username": "alice", "password": "wonderland"}, timeout=5)
    bad = httpx.post(f"{BASE}/login", json={"username": "alice", "password": "nope"}, timeout=5)
    assert ok.status_code == 200 and "token" in ok.json()
    assert bad.status_code == 401
