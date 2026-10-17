"""Sending an image's bytes, as stored, converted or transformed: shared by
GET /images/{id}/content, GET /shared/{token}.{ext} (share links) and GET /signed/... (signed
URLs)."""

import hashlib
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from email.utils import formatdate
from typing import Protocol

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


@dataclass(frozen=True)
class Variant:
    """What a download serves: the stored file (token None), or `spec` applied to it."""

    # The ETag suffix and the variant cache's field; None for the stored file.
    token: str | None
    spec: TransformationSpec | None


class RenderGate(Protocol):
    """Decides whether a render (a variant not in the cache) may run, and for whom."""

    # Whose slots it takes (see ConversionSlots).
    owner: Hashable

    def admit(self) -> None:
        """Before waiting for a slot: raise if no render may start now, counting nothing."""

    def charge(self) -> None:
        """Once the image is read, right before rendering: count the render, or raise."""


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


def conversion(stored_format: str, format: str | None, quality: int | None) -> Variant:
    """The variant a download asking for `format` and `quality` serves (see image_variant)."""
    target_format, effective_quality, token = image_variant(stored_format, format, quality)
    if token is None:
        return Variant(None, None)
    return Variant(token, TransformationSpec(format=target_format, quality=effective_quality))


def transformation_variant(
    stored_format: str, spec: TransformationSpec, transformations: str
) -> Variant:
    """The variant `spec` (its format always set) describes; `transformations` is its one
    canonical spelling (a signed URL's options without the expiry)."""
    if not spec.model_dump(exclude_defaults=True, exclude={"format", "quality"}):
        # Only a format and a quality: the same variant as /content's and share links'.
        return conversion(stored_format, spec.format, spec.quality)
    # Fixed length, whatever the watermark's; apart from conversions' "-q" tokens.
    digest = hashlib.sha256(transformations.encode()).hexdigest()[:32]
    return Variant(f"{spec.format}-t{digest}", spec)


def send_image(
    request: Request,
    image: Image,
    format: str | None,
    quality: int | None,
    **kwargs,
) -> Response:
    """Answer a download of `image` converted to `format`/`quality` (see send_variant)."""
    return send_variant(request, image, conversion(image.format, format, quality), **kwargs)


def send_variant(
    request: Request,
    image: Image,
    variant: Variant,
    *,
    headers: dict[str, str],
    db: Session,
    storage: Storage,
    settings: Settings,
    cache: VariantCache,
    conversion_slots: ConversionSlots | None = None,
    gate: RenderGate | None = None,
) -> Response:
    """Answer a download of `image`, which the caller has authorized: 304 when If-None-Match
    matches, else the stored file streamed, or the variant from the cache or rendered now (in one
    of `conversion_slots`, if given, as `gate` allows). `headers` go on every response."""
    etag = f'"{image.id.hex}"' if variant.token is None else f'"{image.id.hex}-{variant.token}"'
    # The type always comes from the format detected on upload; browsers must not guess another.
    headers = {**headers, "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    release_connection(db)

    if variant.token is None:
        headers["Last-Modified"] = formatdate(image.created_at.timestamp(), usegmt=True)
        return _StoredFileResponse(
            storage.open(image.storage_key), media_type=image.mime_type, headers=headers
        )

    data = cache.get(image.id, variant.token)
    if data is None:
        # A variant too large to cache is rendered for every request, but charged only once.
        charge = None
        if gate is not None and not cache.too_large(image.id, variant.token):
            charge = gate.charge
        if conversion_slots is None:
            data = _render(image, variant, storage, settings, cache, charge)
        else:
            if charge is not None:
                gate.admit()
            with conversion_slots.hold(None if gate is None else gate.owner):
                # A request this one waited for may have just cached the same variant.
                data = cache.get(image.id, variant.token)
                if data is None:
                    data = _render(image, variant, storage, settings, cache, charge)
    return Response(data, media_type=FORMATS[variant.spec.format].mime_type, headers=headers)


def _render(
    image: Image,
    variant: Variant,
    storage: Storage,
    settings: Settings,
    cache: VariantCache,
    charge: Callable[[], None] | None,
) -> bytes:
    source = storage.read(image.storage_key)
    # Not before the read: a storage outage renders nothing, so it must cost nothing.
    if charge is not None:
        charge()
    loaded = load_image(source, settings.max_image_pixels)
    data = apply_transformations(loaded, variant.spec, settings.max_dimension, fast=True).data
    cache.set(image.id, variant.token, data)
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
