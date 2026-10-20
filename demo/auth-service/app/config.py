import os
from dataclasses import dataclass


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
    return Config(
        service_name=os.getenv("SERVICE_NAME", "auth-service"),
        environment=os.getenv("ENVIRONMENT", "local"),
        database_url=os.environ["DATABASE_URL"],
        redis_url=os.environ["REDIS_URL"],
        db_pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        db_max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "2")),
        db_pool_timeout=float(os.getenv("DB_POOL_TIMEOUT", "5")),
        redis_socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "1.0")),
        chaos_enabled=_bool("CHAOS_ENABLED"),
        login_rate_limit_per_minute=int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "1000")),
    )


config = load_config()
