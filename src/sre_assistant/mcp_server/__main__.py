"""Run the MCP server.

uv run python -m sre_assistant.mcp_server                       # stdio (VS Code / Claude Desktop / agents)
uv run python -m sre_assistant.mcp_server --transport streamable-http --port 8765   # remote clients
"""

import argparse
import logging
import sys
from pathlib import Path

from sre_assistant.config import get_settings
from sre_assistant.mcp_server.audit import configure_audit
from sre_assistant.mcp_server.backends.docker_backend import DockerBackend
from sre_assistant.mcp_server.server import build_server

CORPUS = Path(__file__).resolve().parents[3] / "data" / "corpus"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()

    # stdout is the JSON-RPC channel in stdio mode: all diagnostics go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()
    configure_audit(s.mcp_audit_log)

    def retriever_factory():
        from sre_assistant.rag.factory import build_retriever

        return build_retriever(s)

    backend = DockerBackend(s.docker_host, event_store_dsn=s.database_url.get_secret_value())
    server = build_server(backend, retriever_factory, CORPUS, s)
    if a.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
