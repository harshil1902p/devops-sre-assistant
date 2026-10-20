"""Minimal MCP -> LangChain tool adapter (instead of langchain-mcp-adapters, which pins mcp<2).

Each MCP tool becomes a StructuredTool whose:
  - args_schema is the MCP tool's JSON `inputSchema` (the LLM sees exactly what the server validates),
  - content (what the LLM reads) is compact JSON of `structuredContent`,
  - artifact (what our code reads, never sent to the LLM) is the parsed structured result,
  - MCP `isError` results become ToolException -> returned to the model as an error ToolMessage so it can recover.
"""

import json

from langchain_core.tools import StructuredTool, ToolException
from mcp.client import Client

from sre_assistant.guardrails.injection import spotlight


def _content(result) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content, separators=(",", ":"), default=str)
    return "\n".join(getattr(c, "text", "") for c in result.content)


def make_tool(client: Client, name: str, description: str, input_schema: dict, annotations=None) -> StructuredTool:
    async def _call(**kwargs):
        result = await client.call_tool(name, kwargs)
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
