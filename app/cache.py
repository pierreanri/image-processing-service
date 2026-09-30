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

from app.config import get_settings
from app.redis_client import COOLDOWN_SECONDS, RedisOutage, get_redis_client

logger = logging.getLogger(__name__)

# Bump the version whenever encoder output changes (Pillow options, default qualities) so bytes
# produced by the old encoder are never served again.
KEY_PREFIX = "imgsvc:variants:v1"


class VariantCache:
    """Converted variants of an image, kept in one Redis hash per image so that deleting the
    image drops all of them at once. A `None` client disables the cache."""

    def __init__(
        self,
        client: redis.Redis | None,
        *,
        ttl_seconds: int,
        max_item_bytes: int,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._max_item_bytes = max_item_bytes
        self._outage = RedisOutage(
            logger,
            "Variant cache",
            "serving conversions uncached",
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )
        self._writes_rejected = False

    def get(self, image_id: uuid.UUID, variant: str) -> bytes | None:
        if not self._usable():
            return None
        started = self._outage.now()
        try:
            data = self._client.hget(_key(image_id), variant)
        except redis.RedisError as exc:
            self._outage.failed(exc)
            return None
        self._outage.recovered(started)
        return data

    def set(self, image_id: uuid.UUID, variant: str, data: bytes) -> None:
        if len(data) > self._max_item_bytes or not self._usable():
            return
        key = _key(image_id)
        started = self._outage.now()
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
            self._outage.failed(exc)
            return
        self._outage.recovered(started)
        if self._writes_rejected:
            self._writes_rejected = False
            logger.info("Variant cache is accepting writes again")

    def invalidate(self, image_id: uuid.UUID) -> None:
        # Attempted even during a cooldown: deletes are rare and should free the data promptly.
        if self._client is None:
            return
        started = self._outage.now()
        try:
            self._client.unlink(_key(image_id))
        except redis.ResponseError as exc:
            self._write_rejected(exc)
            return
        except redis.RedisError as exc:
            self._outage.failed(exc)
            return
        self._outage.recovered(started)

    def _usable(self) -> bool:
        return self._client is not None and self._outage.usable()

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


@lru_cache
def get_variant_cache() -> VariantCache:
    settings = get_settings()
    return VariantCache(
        get_redis_client(),
        ttl_seconds=settings.cache_ttl_seconds,
        max_item_bytes=settings.cache_max_item_bytes,
    )
