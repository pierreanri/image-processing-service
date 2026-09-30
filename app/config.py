from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
