import logging
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.cache import cache
from app.config import config
from app.database import SessionLocal, hash_password, init_db, pool_status
from app.logging_config import request_id_var

log = logging.getLogger("auth.api")
access_log = logging.getLogger("auth.access")
stats_log = logging.getLogger("auth.runtime")


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _runtime_stats_loop(interval: float = 5.0) -> None:
    while True:
        stats_log.info(
            "runtime stats (db pool: %s)",
            pool_status(),
            extra={"rss_mb": _rss_mb(), "threads": threading.active_count()},
        )
        time.sleep(interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("starting %s (env=%s, chaos=%s)", config.service_name, config.environment, config.chaos_enabled)
    init_db()
    threading.Thread(target=_runtime_stats_loop, daemon=True).start()
    yield
    log.info("shutting down")


app = FastAPI(title="auth-service", lifespan=lifespan)

if config.chaos_enabled:
    from app.chaos import router as chaos_router

    app.include_router(chaos_router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    token = request_id_var.set(request.headers.get("x-request-id", uuid.uuid4().hex[:12]))
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
    except Exception as exc:
        # Handled here (not via app.exception_handler(Exception)) because Starlette's
        # ServerErrorMiddleware re-raises, which double-logs and drops the client connection.
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        response = JSONResponse(status_code=500, content={"error": "internal_error"})
    try:
        status = response.status_code
        return response
    finally:
        if not request.url.path.startswith("/chaos"):
            level = logging.WARNING if status >= 500 else logging.INFO
            access_log.log(
                level,
                "%s %s %s",
                request.method,
                request.url.path,
                status,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
        request_id_var.reset(token)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/healthz")
def healthz():
    """Liveness: the process is up. Deliberately does NOT check dependencies."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness: can we actually serve traffic? Checks DB and Redis."""
    checks = {}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["database"] = f"error: {type(e).__name__}"
    try:
        cache.ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"error: {type(e).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})


@app.post("/login")
def login(body: LoginRequest, request: Request):
    attempts_key = f"login_attempts:{body.username}"
    attempts = cache.incr(attempts_key)
    cache.expire(attempts_key, 60)
    if attempts > config.login_rate_limit_per_minute:
        return JSONResponse(status_code=429, content={"error": "too_many_attempts"})

    with SessionLocal() as db:
        row = db.execute(
            text("SELECT salt, password_hash FROM users WHERE username = :u"), {"u": body.username}
        ).first()

    if row is None or hash_password(body.password, row.salt) != row.password_hash:
        # Realistic (and risky) logging: user-controlled input + client IP end up in logs -> PII and an
        # injection vector. The SRE assistant must pseudonymise/neutralise this before any LLM sees it.
        client = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")
        log.warning("login failed for user %s from %s", body.username[:200], client)
        return JSONResponse(status_code=401, content={"error": "invalid_credentials"})

    token = secrets.token_urlsafe(24)
    cache.setex(f"session:{token}", 3600, body.username)
    return {"token": token, "expires_in": 3600}
