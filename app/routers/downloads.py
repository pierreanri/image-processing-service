"""Sending an image's bytes, as stored or converted: shared by GET /images/{id}/content and
GET /shared/{token}.{ext} (share links)."""

from email.utils import formatdate

from fastapi import Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from app.cache import VariantCache
from app.config import Settings
from app.imaging import DEFAULT_QUALITY, FORMATS, apply_transformations, load_image
from app.models import Image
from app.schemas import TransformationSpec
from app.sharing import ConversionSlots
from app.storage import FileStream, Storage
from app.transforms import release_connection


def image_variant(
    stored_format: str, format: str | None, quality: int | None
) -> tuple[str, int | None, str | None]:
    """What a download asking for `format` and `quality` of an image stored as `stored_format`
    serves: the target format, the quality that applies to it, and the variant token (None for
    the stored file itself)."""
    target_format = format or stored_format
    # Lossless formats ignore quality, so asking for one changes neither the bytes nor the ETag
    # (and a lossless original is served as stored).
    effective_quality = quality if target_format in DEFAULT_QUALITY else None
    if effective_quality is None and target_format == stored_format:
        return target_format, None, None
    # The variant token is both the ETag suffix and the cache field, so they can't drift apart.
    return target_format, effective_quality, f"{target_format}-q{effective_quality or 'default'}"


def send_image(
    request: Request,
    image: Image,
    format: str | None,
    quality: int | None,
    *,
    headers: dict[str, str],
    db: Session,
    storage: Storage,
    settings: Settings,
    cache: VariantCache,
    conversion_slots: ConversionSlots | None = None,
) -> Response:
    """Answer a download of `image`, which the caller has authorized: 304 when If-None-Match
    matches, else the stored file streamed, or its conversion to `format`/`quality` from the
    cache or made now (in one of `conversion_slots`, if given). `headers` go on every
    response."""
    target_format, effective_quality, variant = image_variant(image.format, format, quality)
    etag = f'"{image.id.hex}"' if variant is None else f'"{image.id.hex}-{variant}"'
    # The type always comes from the format detected on upload; browsers must not guess another.
    headers = {**headers, "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    release_connection(db)

    if variant is None:
        headers["Last-Modified"] = formatdate(image.created_at.timestamp(), usegmt=True)
        return _StoredFileResponse(
            storage.open(image.storage_key), media_type=image.mime_type, headers=headers
        )

    data = cache.get(image.id, variant)
    if data is None and conversion_slots is None:
        data = _convert(image, target_format, effective_quality, variant, storage, settings, cache)
    elif data is None:
        with conversion_slots.hold():
            # A request this one waited for may have just cached the same variant.
            data = cache.get(image.id, variant)
            if data is None:
                data = _convert(
                    image, target_format, effective_quality, variant, storage, settings, cache
                )
    return Response(data, media_type=FORMATS[target_format].mime_type, headers=headers)


def _convert(
    image: Image,
    target_format: str,
    quality: int | None,
    variant: str,
    storage: Storage,
    settings: Settings,
    cache: VariantCache,
) -> bytes:
    loaded = load_image(storage.read(image.storage_key), settings.max_image_pixels)
    data = apply_transformations(
        loaded,
        TransformationSpec(format=target_format, quality=quality),
        settings.max_dimension,
    ).data
    cache.set(image.id, variant, data)
    return data


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


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    return etag in (tag.strip().removeprefix("W/") for tag in if_none_match.split(","))
