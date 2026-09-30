import logging
import uuid
from unittest.mock import MagicMock

import pytest
import redis
from pydantic import ValidationError

from app.config import Settings
from app.ratelimit import (
    _SCRIPT,
    KEY_PREFIX,
    UNCHECKED,
    RateLimiter,
    build_transform_rate_limiter,
    transform_limits,
)
from tests.utils import HOUR, MINUTE

USER = uuid.uuid4()
MINUTE_KEY = f"{KEY_PREFIX}:{USER.hex}:minute"
HOUR_KEY = f"{KEY_PREFIX}:{USER.hex}:hour"
FRESH_REPLY = [1, 1, 60_000, 1, 3_600_000]


@pytest.fixture
def redis_mock():
    return MagicMock(spec=redis.Redis)


@pytest.fixture
def script(redis_mock):
    return redis_mock.register_script.return_value


def hit_with(redis_mock, script, reply, limits=(MINUTE, HOUR)):
    script.return_value = reply
    return RateLimiter(redis_mock, limits).hit(USER)


def limiter_log_levels(caplog) -> list[str]:
    return [record.levelname for record in caplog.records if record.name == "app.ratelimit"]


def test_disabled_limiter_never_calls_redis(redis_mock):
    assert RateLimiter(None, (MINUTE, HOUR)).hit(USER) is UNCHECKED
    assert RateLimiter(redis_mock, ()).hit(USER) is UNCHECKED
    redis_mock.register_script.assert_not_called()
    assert UNCHECKED.headers() == {}


def test_hit_runs_one_script_over_every_limit(redis_mock, script):
    hit_with(redis_mock, script, FRESH_REPLY)

    redis_mock.register_script.assert_called_once_with(_SCRIPT)
    script.assert_called_once_with(keys=[MINUTE_KEY, HOUR_KEY], args=[30, 60, 500, 3600])


def test_allowed_hit_reports_every_limit(redis_mock, script):
    decision = hit_with(redis_mock, script, [1, 12, 33_400, 40, 1_799_001])

    assert decision.allowed
    assert decision.retry_after_seconds is None
    assert decision.headers() == {
        "RateLimit-Policy": '"minute";q=30;w=60, "hour";q=500;w=3600',
        "RateLimit": '"minute";r=18;t=34, "hour";r=460;t=1800',
    }


def test_rejected_hit_retries_after_the_used_up_window(redis_mock, script):
    decision = hit_with(redis_mock, script, [0, 30, 12_001, 31, 3_000_000])

    assert not decision.allowed
    assert decision.exceeded == (decision.states[0],)
    assert decision.retry_after_seconds == 13
    assert decision.headers()["Retry-After"] == "13"
    assert decision.headers()["RateLimit"] == '"minute";r=0;t=13, "hour";r=469;t=3000'


def test_retry_after_waits_for_every_used_up_limit(redis_mock, script):
    decision = hit_with(redis_mock, script, [0, 30, 5_000, 500, 900_000])

    assert decision.exceeded == decision.states
    assert decision.headers()["Retry-After"] == "900"


@pytest.mark.parametrize(
    ("pttl_ms", "seconds"), [(0, 1), (1, 1), (999, 1), (1000, 1), (1001, 2), (59_001, 60)]
)
def test_reset_rounds_up_and_is_at_least_one_second(redis_mock, script, pttl_ms, seconds):
    decision = hit_with(redis_mock, script, [1, 1, pttl_ms, 1, 3_600_000])

    assert decision.states[0].reset_seconds == seconds


def test_unstarted_window_is_reported_as_a_full_window(redis_mock, script):
    # A request rejected by the minute limit doesn't create the hour counter.
    decision = hit_with(redis_mock, script, [0, 30, 10_000, 0, -2])

    assert decision.headers()["RateLimit"] == '"minute";r=0;t=10, "hour";r=500;t=3600'


def test_remaining_never_goes_negative(redis_mock, script):
    # Counters above the limit happen when a limit is lowered while they are live.
    decision = hit_with(redis_mock, script, [0, 35, 10_000, 35, 10_000])

    assert [state.remaining for state in decision.states] == [0, 465]


@pytest.mark.parametrize(
    "reply",
    [
        [1, 3],  # counters missing for a limit
        [0, 3, 10_000, 3, 10_000],  # rejected although no limit is used up
    ],
)
def test_inconsistent_replies_raise(redis_mock, script, reply):
    with pytest.raises(ValueError):
        hit_with(redis_mock, script, reply)


def test_only_enabled_limits_are_sent(redis_mock, script):
    decision = hit_with(redis_mock, script, [1, 1, 3_600_000], limits=(HOUR,))

    script.assert_called_once_with(keys=[HOUR_KEY], args=[500, 3600])
    assert decision.headers() == {
        "RateLimit-Policy": '"hour";q=500;w=3600',
        "RateLimit": '"hour";r=499;t=3600',
    }


@pytest.mark.parametrize(
    "error",
    [
        redis.ConnectionError,
        redis.TimeoutError,
        redis.ResponseError,
        redis.exceptions.OutOfMemoryError,
        redis.exceptions.NoPermissionError,
    ],
)
def test_redis_errors_fail_open_during_cooldown(redis_mock, script, clock, caplog, error):
    caplog.set_level(logging.DEBUG, logger="app.ratelimit")
    limiter = RateLimiter(redis_mock, (MINUTE, HOUR), clock=clock)
    script.side_effect = error("boom")

    assert limiter.hit(USER) is UNCHECKED
    assert script.call_count == 1

    # During the cooldown Redis is not called at all.
    clock.advance(4.9)
    assert limiter.hit(USER) is UNCHECKED
    assert script.call_count == 1

    # After it, Redis is tried again; a repeated failure is not logged as a warning.
    clock.advance(0.2)
    assert limiter.hit(USER) is UNCHECKED
    assert script.call_count == 2

    script.side_effect = None
    script.return_value = FRESH_REPLY
    clock.advance(5.1)
    assert limiter.hit(USER).states

    assert limiter_log_levels(caplog) == ["WARNING", "DEBUG", "INFO"]
    assert (
        caplog.records[0]
        .getMessage()
        .startswith(
            f"Transform rate limiter unavailable ({error.__name__}: boom); transformations are"
        )
    )


def test_success_of_a_call_started_before_a_failure_keeps_the_cooldown(
    redis_mock, script, clock, caplog
):
    caplog.set_level(logging.DEBUG, logger="app.ratelimit")
    limiter = RateLimiter(redis_mock, (MINUTE, HOUR), clock=clock)
    calls = []

    def run(keys, args):
        calls.append(keys)
        if len(calls) == 1:
            # While this call is in flight, another thread's call fails.
            clock.advance(0.1)
            assert limiter.hit(uuid.uuid4()) is UNCHECKED
            clock.advance(0.1)
            return FRESH_REPLY
        raise redis.TimeoutError("Timeout reading from socket")

    script.side_effect = run

    assert limiter.hit(USER).states
    assert limiter.hit(USER) is UNCHECKED
    assert len(calls) == 2
    assert limiter_log_levels(caplog) == ["WARNING"]


def test_non_redis_errors_propagate(redis_mock, script):
    script.side_effect = ValueError("bug")

    with pytest.raises(ValueError):
        RateLimiter(redis_mock, (MINUTE, HOUR)).hit(USER)


# --- Settings -------------------------------------------------------------------------------------


def test_rate_limits_default_to_30_per_minute_and_500_per_hour():
    assert transform_limits(Settings(_env_file=None)) == (MINUTE, HOUR)


@pytest.mark.parametrize(
    "variable", ["TRANSFORM_RATE_LIMIT_PER_MINUTE", "TRANSFORM_RATE_LIMIT_PER_HOUR"]
)
def test_negative_rate_limits_are_rejected(monkeypatch, variable):
    monkeypatch.setenv(variable, "-1")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("per_minute", "per_hour", "limits"),
    [(0, 500, (HOUR,)), (30, 0, (MINUTE,)), (0, 0, ())],
)
def test_zero_turns_a_limit_off(per_minute, per_hour, limits):
    settings = Settings(
        _env_file=None,
        transform_rate_limit_per_minute=per_minute,
        transform_rate_limit_per_hour=per_hour,
    )

    assert transform_limits(settings) == limits


def test_limits_without_redis_are_warned_about(caplog):
    caplog.set_level(logging.WARNING, logger="app.ratelimit")

    limiter = build_transform_rate_limiter(Settings(_env_file=None), None)

    assert limiter.hit(USER) is UNCHECKED
    assert [record.getMessage() for record in caplog.records] == [
        "Transformations are not rate limited: REDIS_URL is not set"
    ]


def test_disabled_limits_without_redis_are_not_warned_about(caplog):
    caplog.set_level(logging.WARNING, logger="app.ratelimit")
    settings = Settings(
        _env_file=None, transform_rate_limit_per_minute=0, transform_rate_limit_per_hour=0
    )

    build_transform_rate_limiter(settings, None)

    assert caplog.records == []
