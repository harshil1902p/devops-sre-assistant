import redis

from app.config import config

cache = redis.Redis.from_url(
    config.redis_url,
    socket_timeout=config.redis_socket_timeout,
    socket_connect_timeout=config.redis_socket_timeout,
    decode_responses=True,
)
