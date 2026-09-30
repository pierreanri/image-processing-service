"""S3-compatible storage backend (AWS S3, Cloudflare R2, SeaweedFS, ...).

This is the only module that imports boto3, so deployments on local disk never load it.
"""

import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from urllib.parse import urlsplit

import boto3
import botocore.exceptions
import urllib3.exceptions
from botocore.client import BaseClient
from botocore.config import Config

from app.config import Settings
from app.storage import FileStream, StorageUnavailableError, StoredFile

# S3 answers that mean "try again later" even though their status is not 5xx or 429.
_RETRYABLE_CODES = {"RequestTimeout", "SlowDown"}


def create_s3_client(settings: Settings) -> BaseClient:
    """The process's S3 client: thread-safe, with its own connection pool. Sends no request to S3.

    Raises ValueError for an invalid S3_ENDPOINT_URL and PartialCredentialsError when only one of
    S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY is set. With neither, boto3's default credential
    chain (AWS_* variables, ~/.aws, instance or task roles) is resolved now, which may query the
    instance metadata service; if it finds nothing, RuntimeError is raised, because a client
    built without credentials would never look for them again.
    """
    if settings.s3_endpoint_url:
        _check_endpoint_url(settings.s3_endpoint_url)
    config = Config(
        # botocore waits 60 s by default; a stuck storage call holds one of the API's threads.
        connect_timeout=2,
        read_timeout=10,
        # 3 attempts in all with jittered backoff; standard mode's retry quota stops retrying
        # during a sustained outage. It must be explicit: botocore still defaults to "legacy".
        retries={"mode": "standard", "total_max_attempts": 3},
        # The threadpool's 40 threads plus downloads still streaming to clients.
        max_pool_connections=64,
        # boto3 now sends CRC32 checksums as aws-chunked trailers by default, which several
        # S3-compatible servers reject; uploads carry a signed Content-MD5 instead (see save()).
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        # Only S3_ENDPOINT_URL picks the endpoint, never a stray AWS_ENDPOINT_URL(_S3) variable.
        ignore_configured_endpoint_urls=True,
    )
    # Sessions aren't thread-safe, so this one is only used here, once.
    session = boto3.session.Session()
    explicit_keys = settings.s3_access_key_id or settings.s3_secret_access_key
    if not explicit_keys and session.get_credentials() is None:
        raise RuntimeError(
            "No S3 credentials found: set S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY, or configure "
            "boto3's default chain (AWS_* variables, ~/.aws, an instance or task role)"
        )
    # The client reuses the credentials the session has just resolved.
    return session.client(
        "s3",
        # Custom endpoints automatically get path-style URLs (endpoint/bucket/key).
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region or None,
        aws_access_key_id=settings.s3_access_key_id or None,
        aws_secret_access_key=settings.s3_secret_access_key or None,
        config=config,
    )


def _check_endpoint_url(url: str) -> None:
    # botocore only checks the host when the client is built; a bad scheme or port would
    # otherwise only fail on the first request.
    parts = urlsplit(url)
    try:
        parts.port  # noqa: B018 (raises ValueError for a malformed or out-of-range port)
    except ValueError as exc:
        raise ValueError(f"Invalid S3_ENDPOINT_URL {url!r}: {exc}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Invalid S3_ENDPOINT_URL {url!r}: expected http(s)://host[:port]")


class S3Storage:
    """Stores files as objects in a bucket, under the same keys as LocalStorage. A PUT is atomic,
    so readers never see a partially written object."""

    def __init__(self, client: BaseClient, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def save(self, key: str, data: bytes, *, content_type: str) -> None:
        # The server checks the signed MD5 and rejects a body that was corrupted on the way.
        md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
        with _s3_errors():
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ContentMD5=md5
            )

    def read(self, key: str) -> bytes:
        with _s3_errors():
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            try:
                return body.read()
            finally:
                body.close()

    def open(self, key: str) -> FileStream:
        with _s3_errors():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        return FileStream(response["Body"], response["ContentLength"])

    def delete(self, key: str) -> None:
        with _s3_errors():
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_files(self) -> Iterator[StoredFile]:
        # Needs the s3:ListBucket permission, which nothing else here does.
        with _s3_errors():
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    yield StoredFile(
                        key=item["Key"],
                        size=item["Size"],
                        last_modified=item["LastModified"].astimezone(UTC),
                    )


@contextmanager
def _s3_errors() -> Iterator[None]:
    """Turn failures that a later retry may fix into StorageUnavailableError. Others (a missing
    object or bucket, denied access, bad credentials) propagate unchanged as server errors."""
    try:
        yield
    except (
        # No connection, a timeout, a reset connection or a response cut short.
        botocore.exceptions.ConnectionError,
        botocore.exceptions.HTTPClientError,
        botocore.exceptions.IncompleteReadError,
        # botocore passes these through untranslated (and unretried) when the connection fails
        # while it reads a GetObject error body, or on a TLS error while an object is read.
        urllib3.exceptions.ProtocolError,
        urllib3.exceptions.TimeoutError,
        urllib3.exceptions.SSLError,
    ) as exc:
        raise StorageUnavailableError(f"{type(exc).__name__}: {exc}") from exc
    except botocore.exceptions.ClientError as exc:
        # 5xx and throttling answers that outlived botocore's retries.
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code", "")
        if status >= 500 or status == 429 or code in _RETRYABLE_CODES:
            raise StorageUnavailableError(str(exc)) from exc
        raise
