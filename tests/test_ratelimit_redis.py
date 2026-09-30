"""RateLimiter against a real Redis server: the only tests that run its Lua script.

Uses the redis_client fixture (TEST_REDIS_URL, default redis://localhost:6379/15): skipped when
Redis is unreachable, or failed with REQUIRE_REDIS_TESTS=1. Each test only touches the counters of
fresh random user ids and deletes them afterwards; the database is never flushed.
"""

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.main import app
from app.ratelimit import (
    KEY_PREFIX,
    Limit,
    RateLimitDecision,
    RateLimiter,
    get_transform_rate_limiter,
)
from tests.utils import make_image_bytes, user_id_of


def keys_of(user_id: uuid.UUID) -> tuple[str, str]:
    return f"{KEY_PREFIX}:{user_id.hex}:minute", f"{KEY_PREFIX}:{user_id.hex}:hour"


@pytest.fixture(autouse=True)
def limiter_must_reach_redis(caplog):
    # The limiter fails open, so a broken script or connection would otherwise just allow
    # every request; make any such failure fail the test instead.
    # (Only the test body's records: setup may log that REDIS_URL isn't set for the app itself.)
    caplog.set_level(logging.WARNING, logger="app.ratelimit")
    yield
    failures = [r.getMessage() for r in caplog.get_records("call") if r.name == "app.ratelimit"]
    assert failures == []


@pytest.fixture
def new_user_id(redis_client):
    created = []

    def make() -> uuid.UUID:
        user_id = uuid.uuid4()
        created.append(user_id)
        return user_id

    yield make
    for user_id in created:
        redis_client.unlink(*keys_of(user_id))


def checked(decision: RateLimitDecision) -> RateLimitDecision:
    assert decision.states, "the limiter did not reach Redis"
    return decision


def test_counts_until_the_limit_then_rejects_without_counting(redis_client, new_user_id):
    user_id = new_user_id()
    minute_key, hour_key = keys_of(user_id)
    limiter = RateLimiter(redis_client, (Limit("minute", 3, 60), Limit("hour", 5, 3600)))

    remaining = [checked(limiter.hit(user_id)).states[0].remaining for _ in range(3)]
    assert 3_590_000 < redis_client.pttl(hour_key) <= 3_600_000
    # Shorten both windows so that a rejection re-arming either TTL would show.
    redis_client.pexpire(minute_key, 30_000)
    redis_client.pexpire(hour_key, 1_800_000)
    rejections = [checked(limiter.hit(user_id)) for _ in range(2)]

    assert remaining == [2, 1, 0]
    for decision in rejections:
        assert not decision.allowed
        assert 1 <= decision.retry_after_seconds <= 30
    assert redis_client.get(minute_key) == b"3"
    assert redis_client.get(hour_key) == b"3"
    # Rejections neither count nor extend the window.
    assert 0 < redis_client.pttl(minute_key) <= 30_000
    assert 0 < redis_client.pttl(hour_key) <= 1_800_000


def test_used_up_hour_limit_does_not_consume_the_minute_limit(redis_client, new_user_id):
    user_id = new_user_id()
    minute_key, _ = keys_of(user_id)
    limiter = RateLimiter(redis_client, (Limit("minute", 10, 60), Limit("hour", 2, 3600)))

    assert all(checked(limiter.hit(user_id)).allowed for _ in range(2))
    rejected = checked(limiter.hit(user_id))

    assert not rejected.allowed
    assert [state.limit.name for state in rejected.exceeded] == ["hour"]
    assert 3590 <= rejected.retry_after_seconds <= 3600
    assert redis_client.get(minute_key) == b"2"


def test_window_restarts_when_its_key_expires(redis_client, new_user_id):
    user_id = new_user_id()
    minute_key, _ = keys_of(user_id)
    limiter = RateLimiter(redis_client, (Limit("minute", 1, 60),))
    assert checked(limiter.hit(user_id)).allowed
    assert not checked(limiter.hit(user_id)).allowed

    redis_client.pexpire(minute_key, 1)
    time.sleep(0.02)

    assert checked(limiter.hit(user_id)).allowed
    assert redis_client.get(minute_key) == b"1"
    assert 55_000 < redis_client.pttl(minute_key) <= 60_000


def test_counter_without_a_ttl_gets_one(redis_client, new_user_id):
    user_id = new_user_id()
    minute_key, _ = keys_of(user_id)
    redis_client.set(minute_key, 5)  # e.g. restored or edited by hand, with no expiry
    limiter = RateLimiter(redis_client, (Limit("minute", 1, 60),))

    decision = checked(limiter.hit(user_id))

    assert not decision.allowed
    assert 0 < redis_client.pttl(minute_key) <= 60_000
    assert decision.retry_after_seconds <= 60


def test_users_are_counted_separately(redis_client, new_user_id):
    alice, bob = new_user_id(), new_user_id()
    limiter = RateLimiter(redis_client, (Limit("minute", 1, 60),))

    assert checked(limiter.hit(alice)).allowed
    assert checked(limiter.hit(bob)).allowed
    assert not checked(limiter.hit(alice)).allowed


def test_concurrent_hits_never_exceed_the_limit(redis_client, new_user_id):
    user_id = new_user_id()
    minute_key, _ = keys_of(user_id)
    limiter = RateLimiter(redis_client, (Limit("minute", 10, 60), Limit("hour", 1000, 3600)))

    with ThreadPoolExecutor(max_workers=20) as pool:
        decisions = list(pool.map(lambda _: checked(limiter.hit(user_id)), range(50)))

    assert sum(decision.allowed for decision in decisions) == 10
    assert redis_client.get(minute_key) == b"10"


@pytest.fixture
def endpoint_limiter(client, auth_headers, redis_client):
    limiter = RateLimiter(redis_client, (Limit("minute", 2, 60),))
    app.dependency_overrides[get_transform_rate_limiter] = lambda: limiter
    yield
    redis_client.unlink(*keys_of(user_id_of(auth_headers)))


def test_transform_endpoint_is_limited_end_to_end(client, auth_headers, endpoint_limiter):
    source = client.post(
        "/images", headers=auth_headers, files={"file": ("a.png", make_image_bytes(), "image/png")}
    ).json()
    url = f"/images/{source['id']}/transform"
    flip = {"transformations": {"flip": True}}

    first = client.post(url, headers=auth_headers, json=flip)
    # Neither an unknown image nor an invalid body uses quota.
    unknown = client.post(f"/images/{uuid.uuid4()}/transform", headers=auth_headers, json=flip)
    invalid = client.post(url, headers=auth_headers, json={"transformations": {}})
    second = client.post(url, headers=auth_headers, json=flip)
    third = client.post(url, headers=auth_headers, json=flip)

    assert (first.status_code, unknown.status_code, invalid.status_code) == (201, 404, 422)
    assert first.headers["ratelimit"].startswith('"minute";r=1;t=')
    assert second.status_code == 201
    assert second.headers["ratelimit"].startswith('"minute";r=0;t=')
    assert third.status_code == 429
    retry_after = int(third.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert third.json()["detail"] == (
        f"Too many transformations (limit: 2 per minute); retry in {retry_after}s"
    )
    assert client.get("/images", headers=auth_headers).json()["total"] == 3
