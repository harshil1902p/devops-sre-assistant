"""Drive the real MCP server over the stdio transport against the live demo stack.

Prereqs: docker compose -f demo/compose.yaml up -d (and infra/ + ingest for search_runbooks)
"""

import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
ROOT = Path(__file__).resolve().parents[1]


def _local_env(path: Path) -> dict[str, str]:
    """Read the gitignored local credentials file (created by scripts/bootstrap_local_secrets.py)."""
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def params():
    return StdioServerParameters(command=sys.executable, args=["-m", "sre_assistant.mcp_server"], cwd=str(ROOT))


async def test_stdio_end_to_end(params):
    async with Client(params, read_timeout_seconds=120) as c:
        services = (await c.call_tool("list_services", {})).structured_content["services"]
        health = (await c.call_tool("check_system_health", {"service_name": "auth-service"})).structured_content
        logs = await c.call_tool("inspect_live_logs", {"service_name": "auth-service", "lines_count": 50})
        cfg = await c.call_tool("read_env_config", {"service_name": "auth-service", "filepath": "app.yaml"})
        hits = await c.call_tool("search_runbooks", {"query": "QueuePool limit of size 5 overflow 2 reached"})

    assert {"auth-service", "auth-db", "auth-cache"} <= {s["service"] for s in services}
    assert health["status"] == "running" and any("8080" in p for p in health["ports"])
    assert not logs.is_error
    demo_password = _local_env(ROOT / "demo" / ".env")["AUTH_DB_PASSWORD"]
    assert demo_password not in cfg.structured_content["content"]
    assert hits.structured_content["results"][0]["doc_id"] in {"RB-AUTH-DB-POOL", "PM-INC-2025-0142", "KB-0004"}


async def test_unlabelled_containers_are_invisible(params):
    """Least privilege: the assistant's own vector DB has no sre.service label, so it must not be reachable."""
    async with Client(params, read_timeout_seconds=60) as c:
        services = (await c.call_tool("list_services", {})).structured_content["services"]
        r = await c.call_tool("inspect_live_logs", {"service_name": "vectordb"})
    assert "vectordb" not in {s["service"] for s in services}
    assert r.is_error


async def test_lifecycle_events_are_persisted_by_the_collector():
    """Requires infra/compose.yaml (event-collector) and at least one container start since it ran."""
    import psycopg

    from sre_assistant.config import get_settings

    with psycopg.connect(get_settings().database_url.get_secret_value()) as conn:
        n = conn.execute("SELECT count(*) FROM container_events WHERE service = 'auth-service'").fetchone()[0]
    assert n > 0
