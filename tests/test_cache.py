import logging
import socket
import time
import uuid
from unittest.mock import MagicMock

import pytest
import redis

from app.cache import KEY_PREFIX, VariantCache, create_redis_client

IMAGE_ID = uuid.uuid4()
KEY = f"{KEY_PREFIX}:{IMAGE_ID.hex}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def client():
    return MagicMock(spec=redis.Redis)


@pytest.fixture
def clock():
    return FakeClock()


def make_cache(client, **kwargs) -> VariantCache:
    return VariantCache(client, **{"ttl_seconds": 60, "max_item_bytes": 100, **kwargs})


def pipeline_of(client):
    return client.pipeline.return_value.__enter__.return_value


def cache_log_levels(caplog) -> list[str]:
    return [record.levelname for record in caplog.records if record.name == "app.cache"]


def test_disabled_cache_is_a_no_op():
    cache = make_cache(None)

    assert cache.get(IMAGE_ID, "webp-qdefault") is None
    cache.set(IMAGE_ID, "webp-qdefault", b"data")
    cache.invalidate(IMAGE_ID)


def test_set_writes_variant_into_image_hash_with_ttl(client):
    cache = make_cache(client)

    cache.set(IMAGE_ID, "webp-q80", b"data")

    pipe = pipeline_of(client)
    pipe.hset.assert_called_once_with(KEY, "webp-q80", b"data")
    pipe.expire.assert_called_once_with(KEY, 60)
    pipe.execute.assert_called_once_with()


def test_get_reads_variant_from_image_hash(client):
    client.hget.return_value = b"data"

    assert make_cache(client).get(IMAGE_ID, "webp-q80") == b"data"
    client.hget.assert_called_once_with(KEY, "webp-q80")


def test_set_skips_variants_over_size_cap(client):
    cache = make_cache(client, max_item_bytes=10)

    cache.set(IMAGE_ID, "png-qdefault", b"x" * 11)
    client.pipeline.assert_not_called()

    cache.set(IMAGE_ID, "png-qdefault", b"x" * 10)
    client.pipeline.assert_called_once()


def test_invalidate_unlinks_image_hash(client):
    make_cache(client).invalidate(IMAGE_ID)

    client.unlink.assert_called_once_with(KEY)


@pytest.mark.parametrize("error", [redis.ConnectionError, redis.TimeoutError, redis.ResponseError])
def test_redis_errors_bypass_cache_during_cooldown(client, clock, caplog, error):
    caplog.set_level(logging.DEBUG, logger="app.cache")
    cache = make_cache(client, clock=clock)
    client.hget.side_effect = error("boom")

    assert cache.get(IMAGE_ID, "webp-qdefault") is None
    assert client.hget.call_count == 1

    # During the cooldown Redis is not called at all.
    cache.set(IMAGE_ID, "webp-qdefault", b"data")
    client.pipeline.assert_not_called()
    clock.advance(4.9)
    assert cache.get(IMAGE_ID, "webp-qdefault") is None
    assert client.hget.call_count == 1

    # After the cooldown Redis is tried again; a repeated failure is not logged as a warning.
    clock.advance(0.2)
    assert cache.get(IMAGE_ID, "webp-qdefault") is None
    assert client.hget.call_count == 2

    client.hget.side_effect = None
    client.hget.return_value = b"data"
    clock.advance(5.1)
    assert cache.get(IMAGE_ID, "webp-qdefault") == b"data"

    assert cache_log_levels(caplog) == ["WARNING", "DEBUG", "INFO"]


def test_success_of_a_call_started_before_a_failure_keeps_the_cooldown(client, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="app.cache")
    cache = make_cache(client, clock=clock)

    def hget(key, variant):
        if variant == "slow-but-ok":
            # While this call is in flight, another thread's call fails.
            clock.advance(0.1)
            assert cache.get(IMAGE_ID, "fails") is None
            clock.advance(0.1)
            return b"data"
        raise redis.TimeoutError("Timeout reading from socket")

    client.hget.side_effect = hget

    assert cache.get(IMAGE_ID, "slow-but-ok") == b"data"
    calls = client.hget.call_count
    assert cache.get(IMAGE_ID, "next") is None
    assert client.hget.call_count == calls
    assert cache_log_levels(caplog) == ["WARNING"]


def test_write_timeout_skips_the_item_without_a_cooldown(client, caplog):
    caplog.set_level(logging.DEBUG, logger="app.cache")
    pipeline_of(client).execute.side_effect = redis.TimeoutError("Timeout writing to socket")
    client.hget.return_value = b"cached"
    cache = make_cache(client)

    cache.set(IMAGE_ID, "bmp-qdefault", b"x" * 100)

    assert cache.get(IMAGE_ID, "webp-qdefault") == b"cached"
    assert cache_log_levels(caplog) == ["DEBUG"]


@pytest.mark.parametrize(
    "error", [redis.exceptions.OutOfMemoryError, redis.exceptions.ReadOnlyError]
)
def test_rejected_writes_keep_reads_working_and_warn_once(client, caplog, error):
    caplog.set_level(logging.DEBUG, logger="app.cache")
    pipe = pipeline_of(client)
    pipe.execute.side_effect = error("refused")
    client.hget.return_value = b"cached"
    cache = make_cache(client)

    for _ in range(3):
        cache.set(IMAGE_ID, "webp-qdefault", b"data")
        assert cache.get(IMAGE_ID, "jpeg-q40") == b"cached"

    assert client.hget.call_count == 3
    pipe.execute.side_effect = None
    cache.set(IMAGE_ID, "webp-qdefault", b"data")
    assert cache_log_levels(caplog) == ["WARNING", "DEBUG", "DEBUG", "INFO"]


def test_invalidate_is_attempted_during_cooldown(client, clock):
    cache = make_cache(client, clock=clock)
    client.hget.side_effect = redis.ConnectionError("boom")
    cache.get(IMAGE_ID, "webp-qdefault")

    cache.invalidate(IMAGE_ID)

    client.unlink.assert_called_once_with(KEY)
    # The successful unlink proves Redis is back, which ends the cooldown early.
    client.hget.side_effect = None
    client.hget.return_value = b"data"
    assert cache.get(IMAGE_ID, "webp-qdefault") == b"data"


def test_non_redis_errors_propagate(client):
    client.hget.side_effect = ValueError("bug")

    with pytest.raises(ValueError):
        make_cache(client).get(IMAGE_ID, "webp-qdefault")


def test_client_fails_fast():
    client = create_redis_client("redis://localhost:6379/0", 0.25)

    kwargs = client.get_connection_kwargs()
    assert kwargs["socket_timeout"] == 0.25
    assert kwargs["socket_connect_timeout"] == 0.25
    assert kwargs["protocol"] == 2
    assert client.get_retry().get_retries() == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6379/0",
        "redis://localhost:6379/0?decode_responses=True",
        "redis://localhost:6379/0?socket_timout=1",  # misspelled option
        "redis://localhost:6379/0?protocol=4",
    ],
)
def test_client_rejects_unusable_urls(url):
    with pytest.raises(ValueError):
        create_redis_client(url, 0.25)


def test_refused_connection_is_fast_and_logged_once(closed_port, caplog):
    caplog.set_level(logging.WARNING, logger="app.cache")
    cache = make_cache(create_redis_client(f"redis://127.0.0.1:{closed_port}/0", 0.25))

    started = time.monotonic()
    assert cache.get(IMAGE_ID, "webp-qdefault") is None
    cache.set(IMAGE_ID, "webp-qdefault", b"data")
    cache.invalidate(IMAGE_ID)

    assert time.monotonic() - started < 1
    assert cache_log_levels(caplog) == ["WARNING"]


def test_unresponsive_server_is_bounded_by_timeout_and_not_retried():
    # A listening socket that never accepts: connections complete but nothing ever answers.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = server.getsockname()[1]
        cache = make_cache(create_redis_client(f"redis://127.0.0.1:{port}/0", 0.1))

        started = time.monotonic()
        assert cache.get(IMAGE_ID, "webp-qdefault") is None
        assert time.monotonic() - started < 1

        # Every connection attempt is waiting in the backlog; a retried timeout would add one.
        server.setblocking(False)
        attempts = 0
        while True:
            try:
                server.accept()[0].close()
            except BlockingIOError:
                break
            attempts += 1
        assert attempts == 1
