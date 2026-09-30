"""Redis plumbing shared by the conversion cache and the transform rate limiter.

Both features are best-effort: when Redis is unreachable or slow they log a warning and carry on
without it (see RedisOutage).
"""

import logging
import time
from collections.abc import Callable
from functools import lru_cache

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import get_settings

# After a Redis failure, a feature skips Redis for this long so an outage doesn't add a timeout
# to every request.
COOLDOWN_SECONDS = 5.0


def create_redis_client(url: str, timeout_seconds: float) -> redis.Redis:
    """Build a client that fails fast. Raises ValueError for an unusable URL."""
    client = redis.Redis.from_url(
        url,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
        # One immediate retry on connection errors replaces a pooled socket that went stale.
        # Cache commands are idempotent; a retried rate-limit script can at worst use up one
        # extra slot. Timeouts are not retried.
        retry=Retry(NoBackoff(), 1, supported_errors=(redis.ConnectionError,)),
        # RESP2 skips the RESP3 HELLO and maintenance-notification handshake on each connection.
        protocol=2,
    )
    # Options in the URL's query string override the arguments above.
    if client.get_connection_kwargs().get("decode_responses"):
        raise ValueError("REDIS_URL must not set decode_responses: the cache stores raw bytes")
    pool = client.connection_pool
    try:
        # Building a connection (without opening it) rejects options redis-py doesn't accept,
        # which it would otherwise only report on the first command.
        pool.connection_class(**pool.connection_kwargs)
    except (TypeError, redis.RedisError) as exc:
        raise ValueError(f"Invalid REDIS_URL option: {exc}") from exc
    return client


@lru_cache
def get_redis_client() -> redis.Redis | None:
    """The process's Redis client (thread-safe, with its own connection pool), or None when
    REDIS_URL is empty."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    return create_redis_client(settings.redis_url, settings.redis_timeout_seconds)


class RedisOutage:
    """Tracks whether a feature should skip Redis after a failure, and logs the transitions:
    one WARNING per outage, DEBUG while it lasts and INFO on recovery."""

    def __init__(
        self,
        logger: logging.Logger,
        feature: str,
        consequence: str,
        *,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._feature = feature
        self._consequence = consequence
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        # Shared by the threadpool's threads without a lock: a race costs at most an extra
        # Redis call or log line.
        self._down_until = 0.0
        self._failed_at = float("-inf")
        self._down = False

    def now(self) -> float:
        return self._clock()

    def usable(self) -> bool:
        return self._clock() >= self._down_until

    def failed(self, exc: redis.RedisError) -> None:
        self._failed_at = self._clock()
        self._down_until = self._failed_at + self._cooldown_seconds
        if self._down:
            self._logger.debug(
                "%s still unavailable (%s: %s)", self._feature, type(exc).__name__, exc
            )
            return
        self._down = True
        self._logger.warning(
            "%s unavailable (%s: %s); %s, retrying in %gs",
            self._feature,
            type(exc).__name__,
            exc,
            self._consequence,
            self._cooldown_seconds,
        )

    def recovered(self, started: float) -> None:
        """Record a successful Redis call that started at `started` (from now())."""
        # Only a call issued after the latest failure proves Redis is back; one that was already
        # in flight when another thread's call failed must not cancel the cooldown.
        if started < self._failed_at:
            return
        self._down_until = 0.0
        if self._down:
            self._down = False
            self._logger.info("%s available again", self._feature)
