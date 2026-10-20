"""Fault injection endpoints (only mounted when CHAOS_ENABLED=true).

They are intentionally *silent*: no log lines and excluded from the access log, so the
assistant has to diagnose from symptoms, the same way you would in a real incident.

- slow-queries : long-running queries hold pooled DB connections  -> QueuePool exhaustion
- redis-pause  : Redis stops answering clients (like a blocking KEYS *) -> redis TimeoutError
- memory-leak  : unbounded in-process cache grows until the container is OOMKilled (exit 137)
"""

import threading
import time

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.cache import cache
from app.database import SessionLocal

router = APIRouter(prefix="/chaos", include_in_schema=False)

_leak: list[bytearray] = []
_leak_running = threading.Event()


def _hold_connection(seconds: float) -> None:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
    except Exception:  # noqa: BLE001 - chaos threads must never crash the app
        pass


@router.post("/slow-queries")
def slow_queries(count: int = Query(10, ge=1, le=50), seconds: float = Query(20, gt=0, le=120)):
    for _ in range(count):
        threading.Thread(target=_hold_connection, args=(seconds,), daemon=True).start()
    return {"started": count, "seconds": seconds}


@router.post("/redis-pause")
def redis_pause(seconds: float = Query(15, gt=0, le=120)):
    cache.execute_command("CLIENT", "PAUSE", int(seconds * 1000), "ALL")
    return {"paused_ms": int(seconds * 1000)}


def _grow(mb_per_sec: int) -> None:
    while _leak_running.is_set():
        _leak.append(bytearray(b"x" * (mb_per_sec * 1024 * 1024)))
        time.sleep(1)


@router.post("/memory-leak")
def memory_leak(mb_per_sec: int = Query(25, ge=1, le=200)):
    if not _leak_running.is_set():
        _leak_running.set()
        threading.Thread(target=_grow, args=(mb_per_sec,), daemon=True).start()
    return {"leaking_mb_per_sec": mb_per_sec}


@router.post("/reset")
def reset():
    _leak_running.clear()
    _leak.clear()
    return {"reset": True}
