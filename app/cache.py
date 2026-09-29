"""Best-effort Redis cache for images converted on the fly (GET /images/{id}/content?format=).

The cache can make a conversion faster but never make it fail: every Redis error is logged and
treated as a miss. When Redis can't be reached or doesn't answer in time, it is skipped for a
short cooldown so an outage doesn't add a timeout to every request.
"""

import logging
import time
import uuid
from collections.abc import Callable
from functools import lru_cache

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import get_settings

logger = logging.getLogger(__name__)

# Bump the version whenever encoder output changes (Pillow options, default qualities) so bytes
# produced by the old encoder are never served again.
KEY_PREFIX = "imgsvc:variants:v1"
RETRY_AFTER_SECONDS = 5.0


class VariantCache:
    """Converted variants of an image, kept in one Redis hash per image so that deleting the
    image drops all of them at once. A `None` client disables the cache."""

    def __init__(
        self,
        client: redis.Redis | None,
        *,
        ttl_seconds: int,
        max_item_bytes: int,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._max_item_bytes = max_item_bytes
        self._retry_after_seconds = retry_after_seconds
        self._clock = clock
        # Shared by the threadpool's threads without a lock: a race costs at most an extra
        # Redis call or log line.
        self._down_until = 0.0
        self._failed_at = float("-inf")
        self._down = False
        self._writes_rejected = False

    def get(self, image_id: uuid.UUID, variant: str) -> bytes | None:
        if not self._usable():
            return None
        started = self._clock()
        try:
            data = self._client.hget(_key(image_id), variant)
        except redis.RedisError as exc:
            self._failed(exc)
            return None
        self._recovered(started)
        return data

    def set(self, image_id: uuid.UUID, variant: str, data: bytes) -> None:
        if len(data) > self._max_item_bytes or not self._usable():
            return
        key = _key(image_id)
        started = self._clock()
        try:
            # MULTI/EXEC, so the hash never exists without a TTL.
            with self._client.pipeline() as pipe:
                pipe.hset(key, variant, data)
                pipe.expire(key, self._ttl_seconds)
                pipe.execute()
        except redis.TimeoutError as exc:
            # The get() just before this succeeded, so a timeout here is most likely a large
            # value on a slow link rather than an outage: skip caching it, keep the cache on.
            logger.debug("Variant not cached, write timed out (%d bytes): %s", len(data), exc)
            return
        except redis.ResponseError as exc:
            self._write_rejected(exc)
            return
        except redis.RedisError as exc:
            self._failed(exc)
            return
        self._recovered(started)
        if self._writes_rejected:
            self._writes_rejected = False
            logger.info("Variant cache is accepting writes again")

    def invalidate(self, image_id: uuid.UUID) -> None:
        # Attempted even during a cooldown: deletes are rare and should free the data promptly.
        if self._client is None:
            return
        started = self._clock()
        try:
            self._client.unlink(_key(image_id))
        except redis.ResponseError as exc:
            self._write_rejected(exc)
            return
        except redis.RedisError as exc:
            self._failed(exc)
            return
        self._recovered(started)

    def _usable(self) -> bool:
        return self._client is not None and self._clock() >= self._down_until

    def _failed(self, exc: redis.RedisError) -> None:
        self._failed_at = self._clock()
        self._down_until = self._failed_at + self._retry_after_seconds
        if self._down:
            logger.debug("Variant cache still unavailable (%s: %s)", type(exc).__name__, exc)
            return
        self._down = True
        logger.warning(
            "Variant cache unavailable (%s: %s); serving conversions uncached, retrying in %gs",
            type(exc).__name__,
            exc,
            self._retry_after_seconds,
        )

    def _recovered(self, started: float) -> None:
        # Only a call issued after the latest failure proves Redis is back; one that was already
        # in flight when another thread's call failed must not cancel the cooldown.
        if started < self._failed_at:
            return
        self._down_until = 0.0
        if self._down:
            self._down = False
            logger.info("Variant cache available again")

    def _write_rejected(self, exc: redis.ResponseError) -> None:
        # Redis answered but refused the write (e.g. out of memory under noeviction, or a
        # read-only replica). Reads still work, so this doesn't start the cooldown.
        if self._writes_rejected:
            logger.debug("Variant cache still rejecting writes (%s: %s)", type(exc).__name__, exc)
            return
        self._writes_rejected = True
        logger.warning(
            "Variant cache is rejecting writes (%s: %s); new conversions are not being cached",
            type(exc).__name__,
            exc,
        )


def _key(image_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:{image_id.hex}"


def create_redis_client(url: str, timeout_seconds: float) -> redis.Redis:
    """Build a client that fails fast. Raises ValueError for an unusable URL."""
    client = redis.Redis.from_url(
        url,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
        # One immediate retry on connection errors replaces a pooled socket that went stale
        # (every command we send is idempotent); timeouts are not retried.
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
def get_variant_cache() -> VariantCache:
    settings = get_settings()
    client = (
        create_redis_client(settings.redis_url, settings.redis_timeout_seconds)
        if settings.redis_url
        else None
    )
    return VariantCache(
        client, ttl_seconds=settings.cache_ttl_seconds, max_item_bytes=settings.cache_max_item_bytes
    )
