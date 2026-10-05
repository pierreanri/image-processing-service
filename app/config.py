from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables or a `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc"

    # No default on purpose: the service refuses to start without a real secret.
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)

    # Where image files are kept: "local" (STORAGE_DIR) or "s3" (the S3_* settings).
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = Path("./storage")
    # For STORAGE_BACKEND=s3. Empty values use boto3's defaults: AWS's endpoint, and the region
    # and credentials from its usual chain (AWS_* variables, ~/.aws, instance or task roles).
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    # Largest width/height a transformation may produce.
    max_dimension: int = Field(default=10_000, gt=0)
    # Largest pixel count accepted on upload (guards against decompression bombs).
    max_image_pixels: int = Field(default=50_000_000, gt=0)

    # Optional Redis for the conversion cache and the transform rate limits; empty disables both.
    redis_url: str = ""
    redis_timeout_seconds: float = Field(default=0.25, gt=0)
    cache_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    # Converted images larger than this are served but not cached.
    cache_max_item_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # Per-user limits on POST /images/{id}/transform, counted in Redis (not enforced while
    # REDIS_URL is empty or Redis is down). 0 turns a limit off.
    transform_rate_limit_per_minute: int = Field(default=30, ge=0)
    transform_rate_limit_per_hour: int = Field(default=500, ge=0)

    # Background transformations (POST /images/{id}/transform with Prefer: respond-async).
    # How often an idle worker looks for jobs.
    job_poll_seconds: float = Field(default=1.0, gt=0)
    # How long a worker may take over a job; after that its result is discarded and another
    # worker may take the job over.
    job_lease_seconds: int = Field(default=300, gt=0)
    # Runs of a job (including retries after storage outages) before it is failed.
    job_max_attempts: int = Field(default=3, ge=1)
    # Finished jobs are deleted after this many days.
    job_retention_days: int = Field(default=7, gt=0)

    # Share links (POST /images/{id}/share-links). The longest lifetime a link may be given; links
    # last 1 day by default (or this, if shorter). They are signed with a key derived from
    # JWT_SECRET, so changing it ends every link.
    share_max_ttl_seconds: int = Field(default=30 * 24 * 60 * 60, gt=0, le=365 * 24 * 60 * 60)
    # How many bytes of images each user may store, unless they have a limit of their own
    # (python -m app.quota set); 0 means no limit.
    storage_quota_bytes: int = Field(default=1024**3, ge=0)

    # Conversions (format/quality) that downloads through share links may run at once in each
    # API process; as many more wait up to 5 s for a slot, the rest get 503.
    share_max_concurrent_conversions: int = Field(default=2, gt=0)

    # Webhooks (PUT /me/webhook). The URL clients reach the API at, such as
    # https://api.example.com: webhook payloads link to jobs and images, and workers send them
    # without a request to take the URL from. Empty turns webhooks off.
    public_base_url: str = ""
    # Threads each worker sends webhooks with, beside the one that runs jobs (0: this worker
    # sends none).
    webhook_senders: int = Field(default=4, ge=0, le=8)
    # How long one attempt to deliver an event may take in all: resolving the host, connecting,
    # sending, and receiving the response's headers.
    webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    # Development only: also allow http:// URLs, and hosts on private, loopback and similar
    # networks (cloud metadata addresses stay refused). Never set it where users aren't trusted:
    # it lets them make workers send requests to internal services.
    webhook_allow_private_networks: bool = False

    @field_validator("public_base_url")
    @classmethod
    def _check_public_base_url(cls, value: str) -> str:
        if not value:
            return value
        parts = urlsplit(value)
        try:
            parts.port  # noqa: B018 (raises ValueError for a malformed or out-of-range port)
        except ValueError as exc:
            raise ValueError(f"Invalid PUBLIC_BASE_URL {value!r}: {exc}") from None
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError(
                f"Invalid PUBLIC_BASE_URL {value!r}: expected http(s)://host[:port][/path]"
            )
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
