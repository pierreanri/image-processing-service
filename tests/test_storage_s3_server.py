"""S3Storage against a real S3-compatible server; skipped when none is reachable.

Uses TEST_S3_ENDPOINT_URL (default http://localhost:8333, the SeaweedFS started by
`docker compose --profile s3 up -d s3`), bucket TEST_S3_BUCKET (default imgsvc-test, which compose
creates) and TEST_S3_ACCESS_KEY_ID / TEST_S3_SECRET_ACCESS_KEY (default: compose's development
keys). With REQUIRE_S3_TESTS=1 an unreachable server fails the tests instead of skipping them.

Only a real server checks signatures and Content-MD5 (moto does neither). The tests only touch
objects under random key prefixes and delete them afterwards; they never list, empty or delete
the whole bucket.
"""

import os
import socket
import uuid
from urllib.parse import urlsplit

import botocore.exceptions
import pytest

from app.config import Settings
from app.main import app
from app.storage import CHUNK_SIZE, StorageUnavailableError, build_storage, get_storage
from app.storage_s3 import S3Storage
from tests.utils import make_image_bytes, user_id_of

TEST_S3_ENDPOINT_URL = os.environ.get("TEST_S3_ENDPOINT_URL", "http://localhost:8333")
TEST_S3_BUCKET = os.environ.get("TEST_S3_BUCKET", "imgsvc-test")
TEST_S3_ACCESS_KEY_ID = os.environ.get("TEST_S3_ACCESS_KEY_ID", "imgsvc")
TEST_S3_SECRET_ACCESS_KEY = os.environ.get("TEST_S3_SECRET_ACCESS_KEY", "imgsvc-dev-secret")


def server_settings(**overrides) -> Settings:
    values = {
        "storage_backend": "s3",
        "s3_endpoint_url": TEST_S3_ENDPOINT_URL,
        "s3_bucket": TEST_S3_BUCKET,
        "s3_region": "us-east-1",
        "s3_access_key_id": TEST_S3_ACCESS_KEY_ID,
        "s3_secret_access_key": TEST_S3_SECRET_ACCESS_KEY,
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture(scope="module")
def server_storage() -> S3Storage:
    storage = build_storage(server_settings())
    url = urlsplit(TEST_S3_ENDPOINT_URL)
    try:
        # A quick TCP check first: with nothing listening, botocore would retry with backoff.
        socket.create_connection((url.hostname, url.port or 80), timeout=0.5).close()
        storage.client.head_bucket(Bucket=TEST_S3_BUCKET)
    except (OSError, botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        message = f"S3 bucket {TEST_S3_BUCKET} not reachable at {TEST_S3_ENDPOINT_URL}: {exc}"
        if os.environ.get("REQUIRE_S3_TESTS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return storage


@pytest.fixture
def new_key(server_storage):
    prefix = f"imgsvc-tests/{uuid.uuid4().hex}/"
    created = []

    def make() -> str:
        key = f"{prefix}{uuid.uuid4().hex}.png"
        created.append(key)
        return key

    yield make
    for key in created:
        server_storage.client.delete_object(Bucket=TEST_S3_BUCKET, Key=key)


def error_code(error: botocore.exceptions.ClientError) -> str:
    return error.response["Error"]["Code"]


def test_save_read_stream_and_delete(server_storage, new_key):
    key = new_key()
    data = os.urandom(3 * CHUNK_SIZE + 1)

    server_storage.save(key, data, content_type="image/png")

    assert server_storage.read(key) == data
    file = server_storage.open(key)
    assert file.size == len(data)
    assert b"".join(file) == data
    head = server_storage.client.head_object(Bucket=TEST_S3_BUCKET, Key=key)
    assert head["ContentType"] == "image/png"

    server_storage.delete(key)
    server_storage.delete(key)
    with pytest.raises(botocore.exceptions.ClientError) as missing:
        server_storage.read(key)
    assert error_code(missing.value) == "NoSuchKey"


def test_missing_objects_and_buckets_are_errors_not_outages(server_storage, new_key):
    missing_bucket = S3Storage(server_storage.client, f"imgsvc-missing-{uuid.uuid4().hex[:12]}")

    with pytest.raises(botocore.exceptions.ClientError) as missing_key:
        server_storage.read(new_key())
    with pytest.raises(botocore.exceptions.ClientError) as no_bucket:
        missing_bucket.read(new_key())

    assert error_code(missing_key.value) == "NoSuchKey"
    assert error_code(no_bucket.value) == "NoSuchBucket"


def test_wrong_credentials_are_rejected(server_storage, new_key):
    storage = build_storage(server_settings(s3_secret_access_key="not-the-secret"))

    with pytest.raises(botocore.exceptions.ClientError) as rejected:
        storage.save(new_key(), b"data", content_type="image/png")

    assert not isinstance(rejected.value, StorageUnavailableError)
    assert rejected.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403


def test_server_rejects_an_upload_that_does_not_match_its_md5(server_storage, new_key):
    key = new_key()
    md5_of_empty_body = "1B2M2Y8AsgTpgAmY7PhCfg=="

    with pytest.raises(botocore.exceptions.ClientError) as rejected:
        server_storage.client.put_object(
            Bucket=TEST_S3_BUCKET, Key=key, Body=b"not empty", ContentMD5=md5_of_empty_body
        )

    assert error_code(rejected.value) in {"BadDigest", "InvalidDigest"}
    with pytest.raises(botocore.exceptions.ClientError):
        server_storage.client.head_object(Bucket=TEST_S3_BUCKET, Key=key)


@pytest.fixture
def api_on_server(client, auth_headers, server_storage):
    app.dependency_overrides[get_storage] = lambda: server_storage
    yield
    # The API stores files under "{user id}/"; delete whatever a failed test left there.
    prefix = f"{user_id_of(auth_headers)}/"
    listing = server_storage.client.list_objects_v2(Bucket=TEST_S3_BUCKET, Prefix=prefix)
    for item in listing.get("Contents", []):
        server_storage.client.delete_object(Bucket=TEST_S3_BUCKET, Key=item["Key"])


def test_images_round_trip_through_the_api(client, auth_headers, server_storage, api_on_server):
    data = make_image_bytes(size=(120, 80))
    image = client.post(
        "/images", headers=auth_headers, files={"file": ("a.png", data, "image/png")}
    ).json()

    original = client.get(image["url"], headers=auth_headers)
    webp = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    derived = client.post(
        f"/images/{image['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"flip": True}},
    ).json()
    derived_content = client.get(derived["url"], headers=auth_headers)

    assert original.content == data
    assert original.headers["content-length"] == str(len(data))
    assert webp.status_code == 200
    assert webp.headers["content-type"] == "image/webp"
    assert derived_content.status_code == 200

    for image_id in (image["id"], derived["id"]):
        assert client.delete(f"/images/{image_id}", headers=auth_headers).status_code == 204
    prefix = f"{user_id_of(auth_headers)}/"
    listing = server_storage.client.list_objects_v2(Bucket=TEST_S3_BUCKET, Prefix=prefix)
    assert listing["KeyCount"] == 0
