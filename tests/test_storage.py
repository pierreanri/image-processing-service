import asyncio
import base64
import hashlib
import io
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

import botocore.exceptions
import pytest
import sqlalchemy.orm
import urllib3.exceptions
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_db
from app.main import app
from app.routers.downloads import _StoredFileResponse
from app.storage import (
    CHUNK_SIZE,
    FileStream,
    LocalStorage,
    StorageUnavailableError,
    build_storage,
)
from app.storage_s3 import S3Storage, create_s3_client
from tests.utils import make_image_bytes, stored_keys

KEY = f"{uuid.uuid4()}/{uuid.uuid4().hex}.png"


@pytest.fixture(params=["local", "s3"])
def any_storage(request):
    return request.getfixturevalue(f"{request.param}_storage")


def s3_settings(**overrides) -> Settings:
    values = {
        "storage_backend": "s3",
        "s3_bucket": "imgsvc",
        "s3_region": "us-east-1",
        "s3_access_key_id": "testing",
        "s3_secret_access_key": "testing",
    }
    return Settings(_env_file=None, **(values | overrides))


def client_error(code: str, status: int) -> botocore.exceptions.ClientError:
    response = {
        "Error": {"Code": code, "Message": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return botocore.exceptions.ClientError(response, "Operation")


# --- The storage contract, on both backends -------------------------------------------------------


def test_saved_data_reads_back(any_storage):
    data = bytes(range(256)) * 1024

    any_storage.save(KEY, data, content_type="image/png")

    assert any_storage.read(KEY) == data
    assert stored_keys(any_storage) == [KEY]


def test_open_streams_the_size_and_the_data_in_chunks(any_storage):
    data = os.urandom(2 * CHUNK_SIZE + 1)
    any_storage.save(KEY, data, content_type="image/png")

    file = any_storage.open(KEY)
    chunks = list(file)

    assert file.size == len(data)
    assert b"".join(chunks) == data
    assert all(0 < len(chunk) <= CHUNK_SIZE for chunk in chunks)
    file.close()  # closing again is harmless


def test_delete_removes_the_file_and_ignores_missing_keys(any_storage):
    any_storage.save(KEY, b"data", content_type="image/png")

    any_storage.delete(KEY)
    any_storage.delete(KEY)

    assert stored_keys(any_storage) == []


def test_list_files_lists_every_file_with_its_size_and_time(any_storage):
    assert list(any_storage.list_files()) == []
    other_key = f"{uuid.uuid4()}/{uuid.uuid4().hex}.webp"
    before = datetime.now(UTC) - timedelta(seconds=5)
    any_storage.save(KEY, b"12345", content_type="image/png")
    any_storage.save(other_key, b"123", content_type="image/webp")
    after = datetime.now(UTC) + timedelta(seconds=5)

    files = sorted(any_storage.list_files(), key=lambda file: file.key)

    assert [(file.key, file.size) for file in files] == sorted([(KEY, 5), (other_key, 3)])
    for file in files:
        assert file.last_modified.utcoffset() == timedelta(0)
        assert before <= file.last_modified <= after


def test_a_missing_file_is_an_error_not_an_outage(any_storage):
    for operation in (any_storage.read, any_storage.open):
        with pytest.raises(Exception) as error:
            operation(KEY)
        assert not isinstance(error.value, StorageUnavailableError)


# --- S3 specifics (moto) --------------------------------------------------------------------------


def test_objects_are_stored_under_their_key_with_their_type(s3_storage):
    s3_storage.save("owner/abc.png", make_image_bytes(), content_type="image/png")

    assert stored_keys(s3_storage) == ["owner/abc.png"]
    head = s3_storage.client.head_object(Bucket=s3_storage.bucket, Key="owner/abc.png")
    assert head["ContentType"] == "image/png"


def test_uploads_send_a_content_md5_and_no_aws_chunked_checksums(s3_storage):
    # Third-party S3 servers often reject boto3's default aws-chunked CRC32 trailers.
    sent = []

    def capture(request, **kwargs):
        sent.append(dict(request.headers))

    events = s3_storage.client.meta.events
    events.register("before-send.s3.PutObject", capture)
    try:
        data = make_image_bytes()
        s3_storage.save(KEY, data, content_type="image/png")
    finally:
        events.unregister("before-send.s3.PutObject", capture)

    headers = {
        name.lower(): value.decode() if isinstance(value, bytes) else value
        for name, value in sent[0].items()
    }
    expected_md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    assert headers["content-md5"] == expected_md5
    assert headers["content-type"] == "image/png"
    assert not [
        name for name in headers if name.startswith(("x-amz-checksum", "x-amz-sdk-checksum"))
    ]
    assert "x-amz-trailer" not in headers
    assert headers.get("content-encoding") != "aws-chunked"


def test_client_is_configured_to_fail_fast_and_stay_compatible():
    config = create_s3_client(s3_settings()).meta.config

    assert (config.connect_timeout, config.read_timeout) == (2, 10)
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
    assert config.max_pool_connections == 64
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


def test_endpoint_variables_from_the_environment_are_ignored(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://elsewhere:1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://elsewhere:2")

    client = create_s3_client(s3_settings())

    assert client.meta.endpoint_url == "https://s3.amazonaws.com"


def test_custom_endpoints_get_path_style_urls():
    client = create_s3_client(s3_settings(s3_endpoint_url="http://s3:8333"))

    url = client.generate_presigned_url("get_object", Params={"Bucket": "imgsvc", "Key": "a/b.png"})

    assert url.startswith("http://s3:8333/imgsvc/a/b.png?")


# --- S3 error mapping -----------------------------------------------------------------------------


def call(storage: S3Storage, operation: str):
    if operation == "save":
        return storage.save(KEY, b"data", content_type="image/png")
    if operation == "list_files":
        return list(storage.list_files())
    return getattr(storage, operation)(KEY)


@pytest.mark.parametrize("operation", ["save", "read", "open", "delete", "list_files"])
@pytest.mark.parametrize(
    "error",
    [
        botocore.exceptions.EndpointConnectionError(endpoint_url="http://s3"),
        botocore.exceptions.ConnectTimeoutError(endpoint_url="http://s3"),
        botocore.exceptions.ReadTimeoutError(endpoint_url="http://s3"),
        botocore.exceptions.ResponseStreamingError(error="connection reset"),
        botocore.exceptions.IncompleteReadError(actual_bytes=1, expected_bytes=2),
        client_error("SlowDown", 503),
        client_error("InternalError", 500),
        client_error("TooManyRequests", 429),
        client_error("RequestTimeout", 400),
        urllib3.exceptions.ProtocolError("Connection broken", ConnectionResetError()),
        urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out."),
        urllib3.exceptions.SSLError("bad record mac"),
    ],
    ids=lambda error: (
        type(error).__name__
        if not isinstance(error, botocore.exceptions.ClientError)
        else error.response["Error"]["Code"]
    ),
)
def test_transient_errors_mean_storage_is_unavailable(operation, error):
    client = MagicMock()
    for method in ("put_object", "get_object", "delete_object"):
        getattr(client, method).side_effect = error

    def failing_pages(**kwargs):
        # Like boto3's, the page iterator only sends requests as it is iterated.
        raise error
        yield

    client.get_paginator.return_value.paginate.side_effect = failing_pages

    with pytest.raises(StorageUnavailableError) as raised:
        call(S3Storage(client, "imgsvc"), operation)

    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        client_error("NoSuchKey", 404),
        client_error("NoSuchBucket", 404),
        client_error("AccessDenied", 403),
        client_error("InvalidAccessKeyId", 403),
        botocore.exceptions.NoCredentialsError(),
    ],
    ids=lambda error: (
        type(error).__name__
        if not isinstance(error, botocore.exceptions.ClientError)
        else error.response["Error"]["Code"]
    ),
)
def test_other_errors_are_raised_unchanged(error):
    client = MagicMock()
    client.get_object.side_effect = error

    with pytest.raises(type(error)) as raised:
        S3Storage(client, "imgsvc").read(KEY)

    assert raised.value is error


def test_list_files_reads_every_page():
    client = MagicMock()
    modified = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    client.get_paginator.return_value.paginate.return_value = iter(
        [
            {"Contents": [{"Key": "a", "Size": 1, "LastModified": modified}]},
            {"Contents": [{"Key": "b", "Size": 2, "LastModified": modified}]},
            {},  # An empty bucket's page has no Contents.
        ]
    )

    files = list(S3Storage(client, "imgsvc").list_files())

    client.get_paginator.assert_called_once_with("list_objects_v2")
    client.get_paginator.return_value.paginate.assert_called_once_with(Bucket="imgsvc")
    assert [file.key for file in files] == ["a", "b"]
    assert [file.size for file in files] == [1, 2]
    assert files[0].last_modified == modified
    assert files[0].last_modified.tzinfo is UTC


def test_local_list_files_includes_temporary_files_and_skips_symlinks(local_storage):
    local_storage.save(KEY, b"data", content_type="image/png")
    temp_key = f"{KEY.split('/')[0]}/.{uuid.uuid4().hex}.png.{uuid.uuid4().hex}.tmp"
    local_storage.path(temp_key).write_bytes(b"partial")
    (local_storage.root / "link.png").symlink_to(local_storage.path(KEY))
    (local_storage.root / "linked-dir").symlink_to(local_storage.path(KEY).parent)

    keys = sorted(file.key for file in local_storage.list_files())

    assert keys == sorted([KEY, temp_key])


def test_local_list_files_on_a_missing_directory_is_empty(tmp_path):
    assert list(LocalStorage(tmp_path / "nothing-here").list_files()) == []


# --- Configuration --------------------------------------------------------------------------------


def test_local_disk_is_the_default(monkeypatch, tmp_path):
    monkeypatch.delenv("STORAGE_BACKEND")

    storage = build_storage(Settings(_env_file=None, storage_dir=tmp_path))

    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path.resolve()


def test_s3_backend_is_built_from_the_settings():
    storage = build_storage(s3_settings(s3_endpoint_url="http://s3:8333"))

    assert isinstance(storage, S3Storage)
    assert storage.bucket == "imgsvc"
    assert storage.client.meta.endpoint_url == "http://s3:8333"


def test_s3_backend_requires_a_bucket():
    with pytest.raises(ValueError, match="S3_BUCKET"):
        build_storage(s3_settings(s3_bucket=""))


def test_unknown_backends_are_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, storage_backend="gcs")


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"s3_endpoint_url": "s3:8333"}, ValueError),
        ({"s3_endpoint_url": "s3://imgsvc"}, ValueError),
        ({"s3_endpoint_url": "htps://localhost:8333"}, ValueError),
        ({"s3_endpoint_url": "http://localhost:83330"}, ValueError),
        ({"s3_endpoint_url": "http://localhost:abc"}, ValueError),
        ({"s3_secret_access_key": ""}, botocore.exceptions.PartialCredentialsError),
    ],
)
def test_bad_s3_settings_fail_when_the_client_is_built(overrides, error):
    with pytest.raises(error):
        build_storage(s3_settings(**overrides))


def test_building_s3_storage_sends_no_request(closed_port):
    # Nothing listens there, so any request would fail.
    storage = build_storage(s3_settings(s3_endpoint_url=f"http://127.0.0.1:{closed_port}"))

    assert isinstance(storage, S3Storage)


# --- Endpoints when storage fails -----------------------------------------------------------------


def upload(client, headers):
    files = {"file": ("a.png", make_image_bytes(size=(100, 50)), "image/png")}
    return client.post("/images", headers=headers, files=files)


def unavailable(*args, **kwargs):
    raise StorageUnavailableError("down")


@pytest.mark.parametrize(
    ("operation", "method"),
    [("upload", "save"), ("download", "open"), ("convert", "read"), ("transform", "read")],
)
def test_storage_outage_is_503(client, auth_headers, storage, caplog, operation, method):
    image = None if operation == "upload" else upload(client, auth_headers).json()
    setattr(storage, method, unavailable)

    if operation == "upload":
        response = upload(client, auth_headers)
    elif operation == "download":
        response = client.get(image["url"], headers=auth_headers)
    elif operation == "convert":
        response = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    else:
        response = client.post(
            f"/images/{image['id']}/transform",
            headers=auth_headers,
            json={"transformations": {"flip": True}},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Image storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers  # only share links get it
    assert [r.levelname for r in caplog.records if r.name == "app.main"] == ["WARNING"]
    if operation == "upload":
        assert client.get("/images", headers=auth_headers).json()["total"] == 0


def test_delete_is_204_even_if_the_file_cannot_be_removed(
    client, auth_headers, storage, variant_cache, caplog
):
    image = upload(client, auth_headers).json()
    client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    key = stored_keys(storage)[0]

    def failing_delete(key):
        raise OSError("disk unhappy")

    storage.delete = failing_delete

    response = client.delete(f"/images/{image['id']}", headers=auth_headers)

    assert response.status_code == 204
    assert client.get(f"/images/{image['id']}", headers=auth_headers).status_code == 404
    assert variant_cache.entries == {}
    errors = [r for r in caplog.records if r.name == "app.transforms"]
    assert [r.levelname for r in errors] == ["ERROR"]
    assert key in errors[0].getMessage()


def test_failed_cleanup_does_not_hide_the_database_error(
    client, auth_headers, storage, caplog, monkeypatch
):
    commit = sqlalchemy.orm.Session.commit

    def failing_commit(self):
        # Only the commit that inserts the image fails (not the read-only ones before it).
        if self.new:
            raise RuntimeError("database went away")
        commit(self)

    def failing_delete(key):
        raise OSError("disk unhappy")

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", failing_commit)
    storage.delete = failing_delete

    with pytest.raises(RuntimeError, match="database went away"):
        upload(client, auth_headers)

    assert [r.levelname for r in caplog.records if r.name == "app.transforms"] == ["ERROR"]


def test_downloads_close_the_stored_file(client, auth_headers, storage):
    image = upload(client, auth_headers).json()
    opened = []
    open_file = storage.open

    def spy(key):
        file = open_file(key)
        opened.append(file)
        return file

    storage.open = spy

    response = client.get(image["url"], headers=auth_headers)

    assert response.status_code == 200
    assert isinstance(opened[0], FileStream)
    assert opened[0]._file.closed


@pytest.fixture
def empty_credential_chain(monkeypatch, tmp_path):
    """boto3's default credential chain finds nothing (instance metadata is already disabled)."""
    for name in list(os.environ):
        if name.startswith(("AWS_ACCESS", "AWS_SECRET", "AWS_SESSION", "AWS_PROFILE")) or name in (
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))


def test_missing_credentials_stop_startup(empty_credential_chain):
    # Otherwise the client would keep no credentials for good and every request would be a 500.
    with pytest.raises(RuntimeError, match="No S3 credentials"):
        build_storage(s3_settings(s3_access_key_id="", s3_secret_access_key=""))


def test_default_credential_chain_is_used_without_keys(empty_credential_chain, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-the-environment")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    storage = build_storage(s3_settings(s3_access_key_id="", s3_secret_access_key=""))

    credentials = storage.client._request_signer._credentials
    assert credentials.access_key == "from-the-environment"


def test_downloads_are_closed_when_the_client_disconnects():
    closed = []

    class Body(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    size = 8 * CHUNK_SIZE
    response = _StoredFileResponse(
        FileStream(Body(os.urandom(size)), size), media_type="image/png", headers={}
    )
    body_messages = []

    async def run() -> None:
        disconnected = asyncio.Event()

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                body_messages.append(message)
                disconnected.set()
                await asyncio.sleep(0.05)

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}, "method": "GET", "headers": []}
        await response(scope, receive, send)
        # Checked while the response (and its half-read iterator) is still alive.
        assert closed

    asyncio.run(run())
    assert len(body_messages) < 8


def test_storage_calls_do_not_hold_a_database_connection(client, auth_headers, storage):
    """While storage is slow, other requests must still get a database connection: the pool is
    smaller than the threadpool."""
    image = upload(client, auth_headers).json()
    engine = create_engine(
        get_settings().database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
    )
    tiny_pool = sessionmaker(bind=engine, expire_on_commit=False)

    def get_db_from_tiny_pool():
        with tiny_pool() as session:
            yield session

    app.dependency_overrides[get_db] = get_db_from_tiny_pool
    storage_called, release_storage = threading.Event(), threading.Event()
    open_file = storage.open

    def slow_open(key):
        storage_called.set()
        release_storage.wait(10)
        return open_file(key)

    storage.open = slow_open
    download = {}
    thread = threading.Thread(
        target=lambda: download.update(response=client.get(image["url"], headers=auth_headers))
    )
    try:
        thread.start()
        assert storage_called.wait(10)

        metadata = client.get(f"/images/{image['id']}", headers=auth_headers)

        assert metadata.status_code == 200
    finally:
        release_storage.set()
        thread.join(10)
        engine.dispose()
    assert download["response"].status_code == 200
