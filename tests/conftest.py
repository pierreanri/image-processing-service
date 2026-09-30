import os
import socket

# Configure the app for tests before anything imports its settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc_test"
)
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters-long"
# Never use a developer's Redis from .env; tests that need Redis set it up themselves.
os.environ["REDIS_URL"] = ""
# Keep images on local disk unless a test picks S3, and never probe for AWS instance metadata.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
# Test the default rate limits whatever the developer's environment says.
os.environ.pop("TRANSFORM_RATE_LIMIT_PER_MINUTE", None)
os.environ.pop("TRANSFORM_RATE_LIMIT_PER_HOUR", None)

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.cache import get_variant_cache  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Base, get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.ratelimit import get_transform_rate_limiter  # noqa: E402
from app.redis_client import create_redis_client  # noqa: E402
from app.storage import LocalStorage, get_storage  # noqa: E402
from app.storage_s3 import S3Storage, create_s3_client  # noqa: E402
from tests.utils import (  # noqa: E402
    FakeClock,
    FakeRateLimiter,
    InMemoryVariantCache,
    register,
)


@pytest.fixture(scope="session")
def engine():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def clean_db(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE images, users CASCADE"))


@pytest.fixture
def local_storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


@pytest.fixture(scope="session")
def moto_s3_client():
    """An S3 client for moto's in-memory S3, built once (building one takes ~0.1 s). Only use it
    through the s3_storage fixture: outside mock_aws() its requests would go to the real AWS."""
    return create_s3_client(
        Settings(
            _env_file=None,
            s3_region="us-east-1",
            s3_access_key_id="testing",
            s3_secret_access_key="testing",
        )
    )


@pytest.fixture
def s3_storage(moto_s3_client):
    """S3Storage on a fresh bucket in moto's in-memory S3 (discarded after the test)."""
    with mock_aws():
        moto_s3_client.create_bucket(Bucket="imgsvc-test")
        yield S3Storage(moto_s3_client, "imgsvc-test")


@pytest.fixture
def storage(local_storage) -> LocalStorage:
    # tests/test_images.py overrides this to run its tests against both backends.
    return local_storage


@pytest.fixture
def variant_cache() -> InMemoryVariantCache:
    return InMemoryVariantCache()


@pytest.fixture
def rate_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def client(clean_db, storage, variant_cache, rate_limiter):
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_variant_cache] = lambda: variant_cache
    app.dependency_overrides[get_transform_rate_limiter] = lambda: rate_limiter
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers(client) -> dict:
    return register(client)


@pytest.fixture
def closed_port():
    """A local port that refuses connections. The socket stays bound (but not listening) for
    the whole test, so no other process can take the port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def redis_client():
    """A client for the real Redis at TEST_REDIS_URL. Tests using it are skipped when it is
    unreachable, or fail when REQUIRE_REDIS_TESTS=1 (they are the only tests that run the rate
    limiter's Lua script)."""
    client = create_redis_client(TEST_REDIS_URL, 0.5)
    try:
        client.ping()
    except redis.RedisError as exc:
        message = f"Redis not reachable at {TEST_REDIS_URL}: {exc}"
        if os.environ.get("REQUIRE_REDIS_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    yield client
    client.close()
