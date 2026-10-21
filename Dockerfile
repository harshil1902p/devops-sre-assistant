# Agent API image: FastAPI + LangGraph agent + MCP server (spawned over stdio inside the same container).
# Multi-stage: dependencies are resolved from uv.lock in a builder; the runtime has no build tools and runs as non-root.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app data/corpus ./data/corpus
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    HOME=/tmp SRE_SPEND_LEDGER_PATH=/tmp/spend.jsonl SRE_MCP_AUDIT_LOG=/tmp/mcp_audit.jsonl \
    DEEPEVAL_TELEMETRY_OPT_OUT=YES HF_HOME=/tmp/hf
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1
CMD ["uvicorn", "sre_assistant.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--timeout-keep-alive", "75"]
