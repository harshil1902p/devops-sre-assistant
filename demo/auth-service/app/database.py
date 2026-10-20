"""Database engine and session factory.

Pool settings come from env vars. The demo deliberately ships with a small pool
(DB_POOL_SIZE=5, DB_MAX_OVERFLOW=2, DB_POOL_TIMEOUT=5) so it can be exhausted under load.
"""

import hashlib
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import config

log = logging.getLogger("auth.database")

engine = create_engine(
    config.database_url,
    pool_size=config.db_pool_size,
    max_overflow=config.db_max_overflow,
    pool_timeout=config.db_pool_timeout,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

DEMO_USERS = {"alice": "wonderland", "bob": "builder", "carol": "singer"}


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 10_000).hex()


def init_db() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS users ("
                " username TEXT PRIMARY KEY, salt TEXT NOT NULL, password_hash TEXT NOT NULL)"
            )
        )
        for user, pw in DEMO_USERS.items():
            salt = f"salt-{user}"
            conn.execute(
                text(
                    "INSERT INTO users (username, salt, password_hash) VALUES (:u, :s, :h)"
                    " ON CONFLICT (username) DO NOTHING"
                ),
                {"u": user, "s": salt, "h": hash_password(pw, salt)},
            )
    log.info(
        "database ready (pool_size=%s, max_overflow=%s, pool_timeout=%ss)",
        config.db_pool_size,
        config.db_max_overflow,
        config.db_pool_timeout,
    )


def pool_status() -> str:
    return engine.pool.status()
