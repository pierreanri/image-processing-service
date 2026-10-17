"""Downloads through signed URLs (see app/signed_urls.py). No bearer token: the URL is the
credential, signed by the account's backend with its URL signing key."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.deps import (
    DbSession,
    RenderRateLimiterDep,
    SettingsDep,
    SignedRenderSlotsDep,
    StorageDep,
    UrlSignerDep,
    VariantCacheDep,
)
from app.imaging import predict_output_size
from app.models import Image, UrlSigningKey
from app.ratelimit import RateLimitDecision, RateLimiter
from app.routers.downloads import send_variant, transformation_variant
from app.signed_urls import InvalidOptions, parse_options

router = APIRouter(prefix="/signed", tags=["signed URLs"])

# Variants never change (images are immutable and the options are signed), so browsers keep
# them long. CDNs check again hourly (a cheap 304), so that a rotated key or a deleted image
# stops being served there within the hour.
MAX_AGE_SECONDS = 30 * 24 * 60 * 60
SHARED_MAX_AGE_SECONDS = 60 * 60
# A dead URL stays dead (key ids and image ids are never reused, expiries are signed): caches
# may keep the answer, so a busy page's dead URL doesn't cost a query per view.
GONE_MAX_AGE_SECONDS = 60 * 60

# Any page may embed or fetch a signed URL: no cookies or other credentials are involved.
_CROSS_ORIGIN = {"Access-Control-Allow-Origin": "*"}
_NO_STORE = {"Cache-Control": "no-store", **_CROSS_ORIGIN}
_GONE = {"Cache-Control": f"public, max-age={GONE_MAX_AGE_SECONDS}", **_CROSS_ORIGIN}
_NOT_FOUND = "Signed URL not found"


@router.get(
    "/{key_id}/{signature}/{signed_path:path}",
    response_class=Response,
    responses={
        200: {"content": {"image/*": {}}, "description": "The image bytes."},
        304: {"description": "Not modified (matching If-None-Match)."},
        404: {"description": "Not a URL this service signed, or not sent exactly as signed."},
        410: {
            "description": "Expired, or revoked: the key was rotated or deleted, or the image "
            "deleted."
        },
        422: {
            "description": "Correctly signed, but the options are invalid or not in canonical "
            "form, or the transformation fails on this image."
        },
        429: {
            "description": "The account's signed URLs rendered too much; retry after `Retry-After`."
        },
        503: {
            "description": "Storage unavailable, or too many renders in progress; retry after "
            "`Retry-After`."
        },
    },
)
def get_signed_image(
    key_id: str,
    signature: str,
    signed_path: str,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    cache: VariantCacheDep,
    signer: UrlSignerDep,
    render_slots: SignedRenderSlotsDep,
    render_limiter: RenderRateLimiterDep,
) -> Response:
    """Download an image transformed as a signed URL's options say (see the README for how to
    build and sign them). The URL must be sent exactly as signed: no percent-encoding and no
    query string."""
    # Starlette routes on the decoded path, and an ignored query string would make a new CDN
    # entry per value: either would give one variant many URLs. Checked without any I/O.
    if b"%" in request.scope.get("raw_path", b"") or request.scope.get("query_string"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{_NOT_FOUND}: send it exactly as signed, without percent-encoding or a query string",
            headers=_NO_STORE,
        )
    path = signer.verify(key_id, signature, signed_path)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND, headers=_NO_STORE)
    try:
        options = parse_options(path.options, path.format)
    except InvalidOptions as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Invalid signed URL options: {exc}",
            headers=_NO_STORE,
        ) from None
    now = signer.now()
    if options.expires is not None and now >= options.expires:
        expired_at = datetime.fromtimestamp(options.expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raise HTTPException(
            status.HTTP_410_GONE, f"Signed URL expired at {expired_at}", headers=_GONE
        )
    # The key must still be the image owner's: one statement, two index lookups. A rotated or
    # deleted key, a deleted image and another account's image look alike.
    image = db.scalar(
        select(Image)
        .join(UrlSigningKey, UrlSigningKey.user_id == Image.owner_id)
        .where(Image.id == path.image_id, UrlSigningKey.key_id == key_id)
    )
    if image is None:
        raise HTTPException(status.HTTP_410_GONE, "Signed URL has been revoked", headers=_GONE)

    variant = transformation_variant(image.format, options.spec, options.transformations)
    gate = None
    if variant.token is not None:
        # Fails what can't be rendered (crop outside the image, too large) from the image's
        # size alone: no storage read, slot or render budget is spent on it.
        width, height = predict_output_size(
            image.width, image.height, variant.spec, variant.spec.format, settings.max_dimension
        )
        gate = _RenderBudget(render_limiter, image.owner_id, _render_cost(image, width, height))

    if options.expires is None:
        max_age, shared_max_age = MAX_AGE_SECONDS, SHARED_MAX_AGE_SECONDS
    else:
        # No cache may serve it past its expiry.
        remaining = int(options.expires - now)
        max_age = min(MAX_AGE_SECONDS, remaining)
        shared_max_age = min(SHARED_MAX_AGE_SECONDS, remaining)
    headers = {
        "Cache-Control": f"public, max-age={max_age}, s-maxage={shared_max_age}, immutable",
        # Embeddable by pages that require it (Cross-Origin-Embedder-Policy: require-corp).
        "Cross-Origin-Resource-Policy": "cross-origin",
        **_CROSS_ORIGIN,
    }
    if options.expires is not None:
        # A search index would keep an expiring URL past its end.
        headers["X-Robots-Tag"] = "noindex"
    return send_variant(
        request,
        image,
        variant,
        headers=headers,
        db=db,
        storage=storage,
        settings=settings,
        cache=cache,
        conversion_slots=render_slots,
        gate=gate,
    )


@router.get("/{rest:path}", include_in_schema=False)
def unknown_signed_path(rest: str) -> None:
    """Any other path under /signed, such as one with too few segments: answered like a URL this
    service didn't sign."""
    raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND, headers=_NO_STORE)


def _render_cost(image: Image, width: int, height: int) -> int:
    """The megapixels a render of `image` as `width`x`height` costs: the output's, or a quarter
    of the source's if that is more (decoding a large image isn't free either)."""
    pixels = max(width * height, image.width * image.height / 4)
    return max(1, math.ceil(pixels / 1_000_000))


@dataclass(frozen=True)
class _RenderBudget:
    """The megapixels a signed URL's render costs its account (see RenderGate)."""

    limiter: RateLimiter
    owner: uuid.UUID
    megapixels: int

    def admit(self) -> None:
        self._enforce(self.limiter.check(self.owner, self.megapixels))

    def charge(self) -> None:
        self._enforce(self.limiter.hit(self.owner, self.megapixels))

    @staticmethod
    def _enforce(decision: RateLimitDecision) -> None:
        if decision.allowed:
            return
        retry_after = decision.retry_after_seconds
        # No RateLimit fields: viewers must not learn the account's usage.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many renders through this account's signed URLs; retry in {retry_after}s",
            headers={"Retry-After": str(retry_after), **_NO_STORE},
        )
