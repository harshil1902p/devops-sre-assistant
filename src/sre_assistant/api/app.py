"""FastAPI app for the SRE agent.

    SRE_API_AUTH=disabled uv run uvicorn sre_assistant.api.app:app --port 8000    # local dev (no auth)
    uv run uvicorn sre_assistant.api.app:app --host 0.0.0.0 --port 8000           # Entra ID auth (default)

Endpoints
    GET  /healthz                         liveness (process up)
    GET  /readyz                          readiness (MCP tools loaded, graph built)
    POST /v1/diagnose                     run a diagnosis; JSON, or SSE when `Accept: text/event-stream`
    GET  /v1/threads/{id}                 latest state of a thread (owner or approver)
    POST /v1/threads/{id}/approval        approve/reject a pending action (SRE.Approve, four-eyes)
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sre_assistant.api.auth import (
    DEV_PRINCIPAL,
    ROLE_APPROVE,
    ROLE_DIAGNOSE,
    AuthError,
    EntraValidator,
    Principal,
    bearer_token,
)
from sre_assistant.api.ratelimit import TokenBucket
from sre_assistant.api.service import AgentService, Forbidden, ThreadNotFound
from sre_assistant.config import Settings, get_settings
from sre_assistant.llm.spend import SpendLimitExceeded
from sre_assistant.telemetry import setup_telemetry

log = logging.getLogger("sre.api")


class DiagnoseRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    service: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
    since_minutes: int = Field(30, ge=5, le=24 * 60)
    thread: str | None = Field(None, pattern=r"^inc-[a-f0-9]{8,32}$", description="continue a conversation")


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field("", max_length=500)


def create_app(settings: Settings | None = None, service: AgentService | None = None, validator=None) -> FastAPI:
    s = settings or get_settings()
    svc = service or AgentService(s)
    limiter = TokenBucket(s.api_rate_per_minute)
    if s.api_auth == "entra":
        if validator is None:
            if not (s.entra_tenant_id and s.api_audience):
                raise RuntimeError("SRE_ENTRA_TENANT_ID and SRE_API_AUDIENCE are required (or SRE_API_AUTH=disabled)")
            validator = EntraValidator(s.entra_tenant_id, s.api_audience)
    else:
        log.warning("API AUTH DISABLED: local development only")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        setup_telemetry(s.appinsights_connection_string)
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="SRE Assistant API", version="0.8.0", lifespan=lifespan)
    if s.appinsights_connection_string:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["cache-control"] = "no-store"  # answers can contain operational data
        return response

    @app.exception_handler(AuthError)
    async def _auth(_: Request, e: AuthError):
        return JSONResponse({"detail": e.detail}, status_code=e.status, headers={"www-authenticate": "Bearer"})

    @app.exception_handler(SpendLimitExceeded)
    async def _spend(_: Request, e: SpendLimitExceeded):
        return JSONResponse({"detail": "LLM spend limit reached; diagnosis unavailable"}, status_code=503)

    def principal(request: Request) -> Principal:
        if s.api_auth == "disabled":
            return DEV_PRINCIPAL
        return validator.validate(bearer_token(request.headers.get("authorization")))

    def require(role: str):
        def dep(p: Annotated[Principal, Depends(principal)]) -> Principal:
            if not p.has(role):
                raise HTTPException(403, f"requires app role {role}")
            return p

        return dep

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        if not svc.ready:
            raise HTTPException(503, "starting")
        return {"status": "ready", "tools": len(svc.tools)}

    @app.post("/v1/diagnose")
    async def diagnose(
        body: DiagnoseRequest, request: Request, p: Annotated[Principal, Depends(require(ROLE_DIAGNOSE))]
    ):
        wait = limiter.take(p.subject)
        if wait:
            raise HTTPException(429, "rate limit exceeded", headers={"retry-after": str(int(wait) + 1)})
        try:
            events = svc.diagnose(p, body.question, body.service, body.since_minutes, body.thread)
            if "text/event-stream" in request.headers.get("accept", ""):
                first = await anext(events)  # surface ownership errors as HTTP errors before streaming starts

                async def sse():
                    yield f"event: {first['event']}\ndata: {json.dumps(first, default=str)}\n\n"
                    async for ev in events:
                        yield f"event: {ev['event']}\ndata: {json.dumps(ev, default=str)}\n\n"

                return StreamingResponse(sse(), media_type="text/event-stream", headers={"x-accel-buffering": "no"})
            result = None
            async for ev in events:
                if ev["event"] == "result":
                    result = ev
            return result
        except ThreadNotFound as e:
            raise HTTPException(404, "thread not found") from e

    @app.get("/v1/threads/{thread}")
    async def get_thread(thread: str, p: Annotated[Principal, Depends(require(ROLE_DIAGNOSE))]):
        try:
            return await svc.thread(p, thread, allow_role=ROLE_APPROVE)
        except ThreadNotFound as e:
            raise HTTPException(404, "thread not found") from e

    @app.post("/v1/threads/{thread}/approval")
    async def approval(thread: str, body: ApprovalRequest, p: Annotated[Principal, Depends(require(ROLE_APPROVE))]):
        try:
            return await svc.decide(p, thread, body.approved, body.comment)
        except ThreadNotFound as e:
            raise HTTPException(404, "thread not found") from e
        except Forbidden as e:
            raise HTTPException(403, str(e)) from e
        except LookupError as e:
            raise HTTPException(409, str(e)) from e

    return app


def __getattr__(name: str):
    # `uvicorn sre_assistant.api.app:app` builds the app lazily; importing this module has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
