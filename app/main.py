import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.cache import get_variant_cache
from app.config import get_settings
from app.imaging import ImageProcessingError
from app.ratelimit import get_transform_rate_limiter
from app.routers import auth, images, jobs, shared
from app.sharing import (
    ACCESS_LOG_FILTER,
    ConversionsBusyError,
    get_share_conversion_slots,
    get_share_signer,
    redact_share_tokens,
)
from app.storage import StorageUnavailableError, get_storage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on missing/invalid configuration instead of on the first request. Building the
    # Redis and S3 clients checks their settings without contacting either service (though S3
    # without explicit keys looks up credentials, e.g. from the instance metadata service).
    get_settings()
    get_storage()
    get_variant_cache()
    get_transform_rate_limiter()
    get_share_signer()
    get_share_conversion_slots()
    # Share links are credentials, and uvicorn logs every request's path (adding the filter
    # again, e.g. on a second startup in tests, does nothing).
    logging.getLogger("uvicorn.access").addFilter(ACCESS_LOG_FILTER)
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
    app.include_router(jobs.router)
    app.include_router(shared.router)

    @app.exception_handler(ImageProcessingError)
    async def image_processing_error(request: Request, exc: ImageProcessingError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(StorageUnavailableError)
    async def storage_unavailable(request: Request, exc: StorageUnavailableError) -> JSONResponse:
        path = redact_share_tokens(request.url.path)
        logger.warning("Image storage unavailable (%s %s): %s", request.method, path, exc)
        return JSONResponse({"detail": "Image storage is temporarily unavailable"}, status_code=503)

    @app.exception_handler(ConversionsBusyError)
    async def conversions_busy(request: Request, exc: ConversionsBusyError) -> JSONResponse:
        # Only downloads through share links are limited, and those may come from any page.
        return JSONResponse(
            {"detail": "Too many conversions in progress; retry shortly"},
            status_code=503,
            headers={
                "Retry-After": "5",
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
