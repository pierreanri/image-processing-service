from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.cache import get_variant_cache
from app.config import get_settings
from app.imaging import ImageProcessingError
from app.ratelimit import get_transform_rate_limiter
from app.routers import auth, images


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on missing/invalid configuration instead of on the first request.
    # (Building the Redis client parses REDIS_URL but does not connect.)
    get_settings()
    get_variant_cache()
    get_transform_rate_limiter()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Image Processing Service",
        version="0.1.0",
        description="Upload images, transform them and retrieve them in different formats.",
        lifespan=lifespan,
    )
    app.include_router(auth.router)
    app.include_router(images.router)

    @app.exception_handler(ImageProcessingError)
    async def image_processing_error(request: Request, exc: ImageProcessingError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
