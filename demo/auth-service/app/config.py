import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/srv/config/app.yaml"))


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Config:
    service_name: str
    environment: str
    database_url: str
    redis_url: str
    db_pool_size: int
    db_max_overflow: int
    db_pool_timeout: float
    redis_socket_timeout: float
    chaos_enabled: bool
    login_rate_limit_per_minute: int


def load_config() -> Config:
    """Precedence (12-factor): environment variable > config file (app.yaml) > built-in default."""
    f = yaml.safe_load(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    db, redis_cfg, sec = f.get("database", {}), f.get("redis", {}), f.get("security", {})

    def pick(env: str, file_value, default):
        return os.getenv(env, file_value if file_value is not None else default)

    return Config(
        service_name=pick("SERVICE_NAME", f.get("service", {}).get("name"), "auth-service"),
        environment=os.getenv("ENVIRONMENT", "local"),
        database_url=pick("DATABASE_URL", db.get("url"), None),
        redis_url=pick("REDIS_URL", redis_cfg.get("url"), None),
        db_pool_size=int(pick("DB_POOL_SIZE", db.get("pool_size"), 5)),
        db_max_overflow=int(pick("DB_MAX_OVERFLOW", db.get("max_overflow"), 2)),
        db_pool_timeout=float(pick("DB_POOL_TIMEOUT", db.get("pool_timeout_seconds"), 5)),
        redis_socket_timeout=float(pick("REDIS_SOCKET_TIMEOUT", redis_cfg.get("socket_timeout_seconds"), 1.0)),
        chaos_enabled=_bool("CHAOS_ENABLED"),
        login_rate_limit_per_minute=int(
            pick("LOGIN_RATE_LIMIT_PER_MINUTE", sec.get("login_rate_limit_per_minute"), 1000)
        ),
    )


config = load_config()
