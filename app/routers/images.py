import logging
import math
import re
import uuid
from email.utils import formatdate
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from starlette.types import Receive, Scope, Send

from app.deps import (
    CurrentUser,
    DbSession,
    SettingsDep,
    StorageDep,
    TransformRateLimiterDep,
    VariantCacheDep,
)
from app.imaging import DEFAULT_QUALITY, FORMATS, apply_transformations, load_image
from app.models import Image, User
from app.ratelimit import RateLimitDecision
from app.schemas import ImageFormat, ImageList, ImageOut, TransformationSpec, TransformRequest
from app.storage import FileStream, Storage, build_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/images", tags=["images"])

# Stored images never change (a transformation creates a new image), so clients may cache
# downloads for a long time; ETags are derived from the image id.
CACHE_CONTROL = "private, max-age=31536000, immutable"


@router.post("", status_code=status.HTTP_201_CREATED)
def upload_image(
    request: Request,
    file: Annotated[UploadFile, File(description="The image file to upload.")],
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> ImageOut:
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"File exceeds the maximum upload size of {settings.max_upload_bytes} bytes",
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")

    loaded = load_image(data, settings.max_image_pixels)
    info = FORMATS[loaded.format]
    image = Image(
        owner_id=user.id,
        storage_key=build_key(user.id, info.extension),
        original_filename=_clean_filename(file.filename, info.extension),
        format=loaded.format,
        mime_type=info.mime_type,
        width=loaded.image.width,
        height=loaded.image.height,
        size_bytes=len(data),
    )
    _persist(db, storage, image, data)
    return _to_out(image, request)


@router.get("")
def list_images(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> ImageList:
    owned = Image.owner_id == user.id
    total = db.scalar(select(func.count()).select_from(Image).where(owned)) or 0
    images = db.scalars(
        select(Image)
        .where(owned)
        .order_by(Image.created_at.desc(), Image.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    return ImageList(
        items=[_to_out(image, request) for image in images],
        page=page,
        limit=limit,
        total=total,
        pages=math.ceil(total / limit),
    )


@router.get("/{image_id}")
def get_image(image_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession) -> ImageOut:
    return _to_out(_get_owned_image(db, user, image_id), request)


@router.get(
    "/{image_id}/content",
    response_class=Response,
    responses={
        200: {"content": {"image/*": {}}, "description": "The image bytes."},
        304: {"description": "Not modified (matching If-None-Match)."},
    },
)
def get_image_content(
    image_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    cache: VariantCacheDep,
    format: Annotated[
        ImageFormat | None, Query(description="Convert to this format on the fly.")
    ] = None,
    quality: Annotated[int | None, Query(ge=1, le=100, description="JPEG/WebP quality.")] = None,
) -> Response:
    """Download an image, optionally converted to another format or quality.

    Originals are streamed from storage; conversions are cached in Redis when it is configured.
    """
    image = _get_owned_image(db, user, image_id)
    target_format = format or image.format
    # Lossless formats ignore quality, so asking for one changes neither the bytes nor the ETag
    # (and a lossless original is served as stored).
    effective_quality = quality if target_format in DEFAULT_QUALITY else None
    serve_original = effective_quality is None and target_format == image.format

    # The variant token is both the ETag suffix and the cache field, so they can't drift apart.
    variant = None if serve_original else f"{target_format}-q{effective_quality or 'default'}"
    etag = f'"{image.id.hex}"' if variant is None else f'"{image.id.hex}-{variant}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    if variant is None:
        headers["Last-Modified"] = formatdate(image.created_at.timestamp(), usegmt=True)
        return _StoredFileResponse(
            storage.open(image.storage_key), media_type=image.mime_type, headers=headers
        )

    data = cache.get(image.id, variant)
    if data is None:
        loaded = load_image(storage.read(image.storage_key), settings.max_image_pixels)
        data = apply_transformations(
            loaded,
            TransformationSpec(format=target_format, quality=effective_quality),
            settings.max_dimension,
        ).data
        cache.set(image.id, variant, data)
    return Response(data, media_type=FORMATS[target_format].mime_type, headers=headers)


@router.post(
    "/{image_id}/transform",
    status_code=status.HTTP_201_CREATED,
    responses={
        429: {
            "description": "Too many transformations; retry after `Retry-After` seconds.",
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying.",
                    "schema": {"type": "integer"},
                }
            },
        }
    },
)
def transform_image(
    image_id: uuid.UUID,
    body: TransformRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    rate_limiter: TransformRateLimiterDep,
) -> ImageOut:
    """Apply transformations to an image and save the result as a new image.

    The source image is left untouched; the new image records it as its `parent_id`.
    Transformations are rate limited per user (by default 30 per minute and 500 per hour);
    over a limit the response is 429 with `Retry-After`.
    """
    source = _get_owned_image(db, user, image_id)
    # Counted only once the request is authenticated, schema-valid and about the user's own
    # image (so 401, request-validation 422 and 404 never use quota); a transformation that
    # fails after this (400/415/422) still counts.
    decision = rate_limiter.hit(user.id)
    if not decision.allowed:
        raise _too_many_transformations(decision)
    response.headers.update(decision.headers())

    loaded = load_image(storage.read(source.storage_key), settings.max_image_pixels)
    result = apply_transformations(loaded, body.transformations, settings.max_dimension)

    image = Image(
        owner_id=user.id,
        parent_id=source.id,
        storage_key=build_key(user.id, result.extension),
        original_filename=_with_extension(source.original_filename, result.extension),
        format=result.format,
        mime_type=result.mime_type,
        width=result.width,
        height=result.height,
        size_bytes=len(result.data),
        transformations=body.transformations.model_dump(mode="json", exclude_defaults=True),
    )
    _persist(db, storage, image, result.data)
    return _to_out(image, request)


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(
    image_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    cache: VariantCacheDep,
) -> None:
    """Delete an image. Images transformed from it are kept."""
    image = _get_owned_image(db, user, image_id)
    storage_key = image.storage_key
    db.delete(image)
    db.commit()
    # The image is gone for the API now; failing to remove its file only wastes space.
    _discard(storage, storage_key)
    # After the commit, so a failed delete never drops a valid cache. Leftovers (Redis down,
    # or a conversion racing the delete) can't be served, because the image lookup 404s first,
    # and they expire with the TTL.
    cache.invalidate(image_id)


def _too_many_transformations(decision: RateLimitDecision) -> HTTPException:
    limits = " and ".join(f"{s.limit.requests} per {s.limit.name}" for s in decision.exceeded)
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many transformations (limit: {limits}); retry in {decision.retry_after_seconds}s",
        headers=decision.headers(),
    )


def _get_owned_image(db: DbSession, user: User, image_id: uuid.UUID) -> Image:
    image = db.get(Image, image_id)
    # Other users' images are reported as missing so their ids can't be probed.
    if image is None or image.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    return image


def _persist(db: DbSession, storage: Storage, image: Image, data: bytes) -> None:
    # File first: a crash in between leaves an orphaned file, never a row without its file.
    storage.save(image.storage_key, data, content_type=image.mime_type)
    db.add(image)
    try:
        db.commit()
    except Exception:
        db.rollback()
        _discard(storage, image.storage_key)
        raise


def _discard(storage: Storage, key: str) -> None:
    """Delete a file whose image is gone, logging (not raising) a failure."""
    try:
        storage.delete(key)
    except Exception:
        logger.exception("Could not delete image file %s; it is left orphaned", key)


class _StoredFileResponse(StreamingResponse):
    """Streams an open stored file with its Content-Length, and always closes it, even when the
    client disconnects halfway (which releases the file or the S3 connection straight away)."""

    def __init__(self, file: FileStream, *, media_type: str, headers: dict[str, str]) -> None:
        super().__init__(
            file, media_type=media_type, headers={**headers, "Content-Length": str(file.size)}
        )
        self._file = file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._file.close()


def _to_out(image: Image, request: Request) -> ImageOut:
    return ImageOut(
        id=image.id,
        parent_id=image.parent_id,
        url=str(request.url_for("get_image_content", image_id=image.id)),
        original_filename=image.original_filename,
        format=image.format,
        mime_type=image.mime_type,
        width=image.width,
        height=image.height,
        size_bytes=image.size_bytes,
        transformations=image.transformations,
        created_at=image.created_at,
    )


def _clean_filename(filename: str | None, extension: str) -> str:
    # Keep only the final path component; browsers on Windows may send full paths.
    name = re.split(r"[\\/]", filename or "")[-1].strip()
    return name[:255] or f"upload.{extension}"


def _with_extension(filename: str, extension: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem[: 254 - len(extension)]}.{extension}"


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    return etag in (tag.strip().removeprefix("W/") for tag in if_none_match.split(","))
