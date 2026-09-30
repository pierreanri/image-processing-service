import math
import re
import uuid
from email.utils import formatdate
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
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
from app.jobs import enqueue
from app.models import Image, User
from app.ratelimit import RateLimitDecision
from app.routers.views import image_out, job_out
from app.schemas import (
    ImageFormat,
    ImageList,
    ImageOut,
    JobOut,
    TransformationSpec,
    TransformRequest,
)
from app.storage import FileStream, build_key
from app.transforms import discard_file, persist_image, release_connection, render_transformation

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
    persist_image(db, storage, image, data)
    return image_out(image, request)


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
        items=[image_out(image, request) for image in images],
        page=page,
        limit=limit,
        total=total,
        pages=math.ceil(total / limit),
    )


@router.get("/{image_id}")
def get_image(image_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession) -> ImageOut:
    return image_out(_get_owned_image(db, user, image_id), request)


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
    release_connection(db)

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
        202: {
            "model": JobOut,
            "description": "Queued as a background job (sent `Prefer: respond-async`).",
            "headers": {
                "Location": {"description": "Where to poll the job.", "schema": {"type": "string"}},
                "Preference-Applied": {
                    "description": "`respond-async`",
                    "schema": {"type": "string"},
                },
            },
        },
        429: {
            "description": "Too many transformations; retry after `Retry-After` seconds.",
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying.",
                    "schema": {"type": "integer"},
                }
            },
        },
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

    With `Prefer: respond-async` the transformation runs in the background instead: the
    response is 202 with a job to poll at its `Location` (GET /jobs/{id}).
    """
    source = _get_owned_image(db, user, image_id)
    release_connection(db)
    # Counted only once the request is authenticated, schema-valid and about the user's own
    # image (so 401, request-validation 422 and 404 never use quota); a transformation that
    # fails after this (400/415/422) still counts.
    decision = rate_limiter.hit(user.id)
    if not decision.allowed:
        raise _too_many_transformations(decision)
    response.headers.update(decision.headers())

    if _prefers_async(request.headers.getlist("prefer")):
        job = enqueue(db, user.id, source.id, body.transformations)
        return JSONResponse(
            job_out(job, request, None).model_dump(mode="json"),
            status_code=status.HTTP_202_ACCEPTED,
            headers={
                **decision.headers(),
                "Location": str(request.url_for("get_job", job_id=job.id)),
                "Preference-Applied": "respond-async",
            },
        )

    image, data = render_transformation(storage, settings, source, body.transformations, user.id)
    persist_image(db, storage, image, data)
    return image_out(image, request)


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
    discard_file(storage, storage_key)
    # After the commit, so a failed delete never drops a valid cache. Leftovers (Redis down,
    # or a conversion racing the delete) can't be served, because the image lookup 404s first,
    # and they expire with the TTL.
    cache.invalidate(image_id)


def _prefers_async(prefer_headers: list[str]) -> bool:
    """Whether the Prefer headers (RFC 7240) include `respond-async`; its parameters, and other
    preferences such as `wait`, are ignored."""
    for header in prefer_headers:
        for preference in header.split(","):
            name = preference.split(";", 1)[0].split("=", 1)[0].strip().lower()
            if name == "respond-async":
                return True
    return False


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


def _clean_filename(filename: str | None, extension: str) -> str:
    # Keep only the final path component; browsers on Windows may send full paths.
    name = re.split(r"[\\/]", filename or "")[-1].strip()
    return name[:255] or f"upload.{extension}"


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    return etag in (tag.strip().removeprefix("W/") for tag in if_none_match.split(","))
