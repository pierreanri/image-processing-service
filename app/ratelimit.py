"""Per-user rate limits on transformations, counted in Redis.

Each limit is a fixed window that starts with a user's first counted request and ends when its
Redis key expires, so every API instance agrees on it. One Lua script checks every limit and
counts the request against all of them or, if any is used up, against none.

Like the conversion cache, the limiter fails open: while Redis is unreachable or slow (or
REDIS_URL is empty) requests are allowed without being counted.
"""

import logging
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import redis

from app.config import Settings, get_settings
from app.redis_client import COOLDOWN_SECONDS, RedisOutage, get_redis_client

logger = logging.getLogger(__name__)

# Bump the version if the counting semantics change.
KEY_PREFIX = "imgsvc:ratelimit:v1:transform"

# KEYS[i] counts one user's requests in the current window of limit i, which allows ARGV[2i-1]
# requests per ARGV[2i] seconds. Returns {allowed (1 or 0), count_1, pttl_ms_1, count_2, ...}.
_SCRIPT = """
local counts, allowed = {}, 1
for i, key in ipairs(KEYS) do
    counts[i] = tonumber(redis.call('GET', key) or '0')
    if counts[i] >= tonumber(ARGV[2 * i - 1]) then
        allowed = 0
    end
end
local reply = {allowed}
for i, key in ipairs(KEYS) do
    if allowed == 1 then
        counts[i] = redis.call('INCR', key)
    end
    local ttl = redis.call('PTTL', key)
    if ttl == -1 then
        -- A new counter, or one that somehow lost its TTL: never lock a user out for good.
        redis.call('EXPIRE', key, ARGV[2 * i])
        ttl = tonumber(ARGV[2 * i]) * 1000
    end
    reply[2 * i] = counts[i]
    reply[2 * i + 1] = ttl
end
return reply
"""


@dataclass(frozen=True)
class Limit:
    name: str
    requests: int
    window_seconds: int


@dataclass(frozen=True)
class LimitState:
    limit: Limit
    remaining: int
    # Seconds until the current window ends (the whole window if it hasn't started).
    reset_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    # Empty when the limits were not checked (no Redis, no limits, or Redis unavailable).
    states: tuple[LimitState, ...] = ()

    @property
    def exceeded(self) -> tuple[LimitState, ...]:
        if self.allowed:
            return ()
        return tuple(state for state in self.states if state.remaining == 0)

    @property
    def retry_after_seconds(self) -> int | None:
        # The request can only succeed once every used-up limit has reset.
        return max((state.reset_seconds for state in self.exceeded), default=None)

    def headers(self) -> dict[str, str]:
        """RateLimit-Policy and RateLimit fields (draft-ietf-httpapi-ratelimit-headers), plus
        Retry-After when the request was rejected."""
        if not self.states:
            return {}
        headers = {
            "RateLimit-Policy": ", ".join(
                f'"{s.limit.name}";q={s.limit.requests};w={s.limit.window_seconds}'
                for s in self.states
            ),
            "RateLimit": ", ".join(
                f'"{s.limit.name}";r={s.remaining};t={s.reset_seconds}' for s in self.states
            ),
        }
        if self.retry_after_seconds is not None:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


UNCHECKED = RateLimitDecision(allowed=True)


class RateLimiter:
    """Counts each user's requests against every limit at once. Without a client or limits it
    allows everything without calling Redis."""

    def __init__(
        self,
        client: redis.Redis | None,
        limits: Sequence[Limit],
        *,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = tuple(limits)
        # Only hashes the script locally; redis-py loads it into Redis on first use.
        self._script = (
            client.register_script(_SCRIPT) if client is not None and self._limits else None
        )
        self._args = [n for limit in self._limits for n in (limit.requests, limit.window_seconds)]
        self._outage = RedisOutage(
            logger,
            "Transform rate limiter",
            "transformations are not rate limited",
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )

    def hit(self, user_id: uuid.UUID) -> RateLimitDecision:
        """Count one request by `user_id`, unless a limit is used up."""
        if self._script is None or not self._outage.usable():
            return UNCHECKED
        keys = [f"{KEY_PREFIX}:{user_id.hex}:{limit.name}" for limit in self._limits]
        started = self._outage.now()
        try:
            reply = self._script(keys=keys, args=self._args)
        except redis.RedisError as exc:
            self._outage.failed(exc)
            return UNCHECKED
        self._outage.recovered(started)
        return self._decision(reply)

    def _decision(self, reply: list[int]) -> RateLimitDecision:
        allowed, *counters = reply
        states = tuple(
            LimitState(limit, max(limit.requests - count, 0), _reset_seconds(pttl_ms, limit))
            for limit, count, pttl_ms in zip(
                self._limits, counters[::2], counters[1::2], strict=True
            )
        )
        decision = RateLimitDecision(allowed == 1, states)
        if not decision.allowed and not decision.exceeded:
            raise ValueError(f"Rejected without a used-up limit: {reply!r}")
        return decision


def _reset_seconds(pttl_ms: int, limit: Limit) -> int:
    if pttl_ms < 0:  # the key doesn't exist: this window hasn't started
        return limit.window_seconds
    # Rounded up, so waiting this long is never too early.
    return max(1, math.ceil(pttl_ms / 1000))


def transform_limits(settings: Settings) -> tuple[Limit, ...]:
    limits = (
        Limit("minute", settings.transform_rate_limit_per_minute, 60),
        Limit("hour", settings.transform_rate_limit_per_hour, 60 * 60),
    )
    return tuple(limit for limit in limits if limit.requests > 0)


def build_transform_rate_limiter(settings: Settings, client: redis.Redis | None) -> RateLimiter:
    limits = transform_limits(settings)
    if limits and client is None:
        logger.warning("Transformations are not rate limited: REDIS_URL is not set")
    return RateLimiter(client, limits)


@lru_cache
def get_transform_rate_limiter() -> RateLimiter:
    return build_transform_rate_limiter(get_settings(), get_redis_client())
