"""End-to-end MCP demo over the real stdio transport: break the service, then diagnose it with tools only.

    uv run python scripts/mcp_demo.py pool     # QueuePool exhaustion
    uv run python scripts/mcp_demo.py oom      # OOMKilled + restart (evidence via events)
    uv run python scripts/mcp_demo.py none     # just inspect

This is exactly the information the LLM agent (Phase 4) will receive.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


def show(title: str, result) -> None:
    print(f"\n━━ {title} {'(ERROR)' if result.is_error else ''}")
    data = result.structured_content or {"text": result.content[0].text}
    print(json.dumps(data, indent=2)[:2500])


async def main(scenario: str) -> None:
    if scenario != "none":
        subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_chaos.py"), scenario, "--seconds", "20"],
                       check=True, cwd=ROOT)  # fmt: skip

    params = StdioServerParameters(command=sys.executable, args=["-m", "sre_assistant.mcp_server"], cwd=str(ROOT))
    async with Client(params, read_timeout_seconds=120) as c:
        print(f"connected to {c.server_info.name} {c.server_info.version}; tools:",
              [t.name for t in (await c.list_tools()).tools])  # fmt: skip

        health = await c.call_tool("check_system_health", {"service_name": "auth-service"})
        show("check_system_health", health)
        logs = await c.call_tool("inspect_live_logs", {"service_name": "auth-service", "lines_count": 1000})
        show("inspect_live_logs", logs)
        events = await c.call_tool("get_recent_events", {"service_name": "auth-service", "since_minutes": 15})
        show("get_recent_events", events)

        groups = (logs.structured_content or {}).get("error_groups") or []
        query = groups[0]["search_hint"] if groups else "container restarted, exit code 137, OOMKilled, memory grows"
        hits = await c.call_tool("search_runbooks", {"query": query, "service": "auth-service", "top_k": 2})
        sc = hits.structured_content or {}
        print(f"\n━━ search_runbooks  query={query[:90]!r}")
        for h in sc.get("results", []):
            print(f"   {h['citation']:<55} score={h['score']}")

        if not logs.is_error:
            lr = logs.structured_content
            print(
                f"\nlog compression: {lr['raw_tokens']} raw tokens -> {lr['output_tokens']} tool-output tokens "
                f"({100 * (1 - lr['output_tokens'] / max(lr['raw_tokens'], 1)):.0f}% smaller)"
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "none"))
