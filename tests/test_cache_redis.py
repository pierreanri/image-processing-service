"""VariantCache against a real Redis server; skipped when none is reachable.

Uses TEST_REDIS_URL (default redis://localhost:6379/15). Each test only touches keys for a fresh
random image id and deletes them afterwards; the database is never flushed.
"""

import os
import uuid

import pytest
import redis

from app.cache import KEY_PREFIX, VariantCache, create_redis_client

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture(scope="module")
def redis_client():
    client = create_redis_client(TEST_REDIS_URL, 0.5)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}: {exc}")
    yield client
    client.close()


@pytest.fixture
def image_id(redis_client):
    image_id = uuid.uuid4()
    yield image_id
    redis_client.unlink(f"{KEY_PREFIX}:{image_id.hex}")


@pytest.fixture
def cache(redis_client) -> VariantCache:
    return VariantCache(redis_client, ttl_seconds=60, max_item_bytes=1024)


def test_variants_round_trip_in_one_hash_with_ttl(redis_client, cache, image_id):
    webp, jpeg = bytes(range(256)) * 3, b"\xff\xd8 binary \x00 data"

    cache.set(image_id, "webp-qdefault", webp)
    cache.set(image_id, "jpeg-q40", jpeg)

    assert cache.get(image_id, "webp-qdefault") == webp
    assert cache.get(image_id, "jpeg-q40") == jpeg
    assert cache.get(image_id, "png-qdefault") is None
    key = f"{KEY_PREFIX}:{image_id.hex}"
    assert redis_client.type(key) == b"hash"
    assert set(redis_client.hkeys(key)) == {b"webp-qdefault", b"jpeg-q40"}
    assert 0 < redis_client.ttl(key) <= 60


def test_items_over_size_cap_are_not_stored(cache, image_id):
    cache.set(image_id, "bmp-qdefault", b"x" * 1025)

    assert cache.get(image_id, "bmp-qdefault") is None


def test_invalidate_removes_every_variant(redis_client, cache, image_id):
    cache.set(image_id, "webp-qdefault", b"a")
    cache.set(image_id, "jpeg-q40", b"b")

    cache.invalidate(image_id)

    assert redis_client.exists(f"{KEY_PREFIX}:{image_id.hex}") == 0
    assert cache.get(image_id, "webp-qdefault") is None
