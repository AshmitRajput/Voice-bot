import os
from typing import Optional

import redis
from redis import Redis


REDIS_URL = os.getenv(
    "REDIS_URL",
    "redis://localhost:6379/0",
)

REDIS_ENABLED = (
    os.getenv("REDIS_ENABLED", "true").lower() == "true"
)

redis_client: Optional[Redis] = None


if REDIS_ENABLED:
    try:
        candidate = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )

        candidate.ping()
        redis_client = candidate

    except redis.RedisError:
        redis_client = None