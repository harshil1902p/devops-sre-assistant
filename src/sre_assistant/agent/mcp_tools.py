"""Minimal MCP -> LangChain tool adapter (instead of langchain-mcp-adapters, which pins mcp<2).

Each MCP tool becomes a StructuredTool whose:
  - args_schema is the MCP tool's JSON `inputSchema` (the LLM sees exactly what the server validates),
  - content (what the LLM reads) is compact JSON of `structuredContent`,
  - artifact (what our code reads, never sent to the LLM) is the parsed structured result,
  - MCP `isError` results become ToolException -> returned to the model as an error ToolMessage so it can recover.
"""

import json
import os

from langchain_core.tools import StructuredTool, ToolException
from mcp.client import Client
from mcp.client.stdio import get_default_environment

from sre_assistant.guardrails.injection import spotlight
from sre_assistant.telemetry import tool_calls, tracer


def _content(result) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, separators=(",", ":"), default=str)
    return "\n".join(getattr(c, "text", "") for c in result.content)


def make_tool(client: Client, name: str, description: str, input_schema: dict, annotations=None) -> StructuredTool:
    async def _call(**kwargs):
        # One span per tool call: name + outcome only. Arguments/results can contain log data, so they stay out
        # of telemetry (privacy) — the audit log on the MCP server side has the redacted details.
        with tracer.start_as_current_span(f"mcp.tool {name}", attributes={"mcp.tool": name}) as span:
            result = await client.call_tool(name, kwargs)
            span.set_attribute("mcp.is_error", bool(result.is_error))
            tool_calls.add(1, {"tool": name, "outcome": "error" if result.is_error else "ok"})
        if result.is_error:
            raise ToolException(_content(result))
        # Spotlighting: the LLM sees tool output as clearly delimited, untrusted DATA.
        return spotlight(name, _content(result)), result.structured_content

    return StructuredTool(
        name=name,
        description=description or name,
        args_schema=input_schema,
        coroutine=_call,
        response_format="content_and_artifact",
        handle_tool_error=lambda e: f"ERROR: {e}",
        metadata={
            "read_only": bool(annotations and annotations.read_only_hint),
            "destructive": bool(annotations and annotations.destructive_hint),
        },
    )


async def load_mcp_tools(client: Client) -> list[StructuredTool]:
    return [
        make_tool(client, t.name, t.description, t.input_schema, t.annotations)
        for t in (await client.list_tools()).tools
    ]


# Env vars a stdio MCP server needs besides the SDK's minimal safe default (PATH, HOME...): our SRE_* settings and
# what Azure identity needs on each platform. Container Apps / App Service managed identity: IDENTITY_ENDPOINT +
# IDENTITY_HEADER (MSI_* legacy); AKS workload identity: AZURE_FEDERATED_TOKEN_FILE etc. Forgetting these made the
# server's DefaultAzureCredential fail in the cloud while everything worked locally (az login) — see docs 08.
FORWARD_PREFIXES = ("SRE_", "AZURE_", "IDENTITY_", "MSI_", "APPLICATIONINSIGHTS_", "OTEL_", "DEEPEVAL_", "HF_")


def mcp_subprocess_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return get_default_environment() | {k: v for k, v in environ.items() if k.startswith(FORWARD_PREFIXES)}
