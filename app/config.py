from functools import lru_cache
from pathlib import Path

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

    storage_dir: Path = Path("./storage")

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    # Largest width/height a transformation may produce.
    max_dimension: int = Field(default=10_000, gt=0)
    # Largest pixel count accepted on upload (guards against decompression bombs).
    max_image_pixels: int = Field(default=50_000_000, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
