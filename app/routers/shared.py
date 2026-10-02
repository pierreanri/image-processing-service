"""Downloads through share links (see app/sharing.py). No bearer token: the link is the
credential."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.deps import (
    DbSession,
    SettingsDep,
    ShareConversionSlotsDep,
    ShareSignerDep,
    StorageDep,
    VariantCacheDep,
)
from app.imaging import FORMATS
from app.models import Image
from app.routers.downloads import send_image

router = APIRouter(prefix="/shared", tags=["shared"])

# How long a browser may reuse a download before checking that its link still works, which
# bounds how long a revoked link keeps showing where it was already seen. Responses are private,
# so no shared cache (a proxy or CDN) keeps serving a revoked link to anyone else.
MAX_AGE_SECONDS = 60 * 60
# Any page may embed or fetch a shared image: no cookies or other credentials are involved.
_CROSS_ORIGIN = {"Access-Control-Allow-Origin": "*"}
_ERROR_HEADERS = {"Cache-Control": "no-store", **_CROSS_ORIGIN}


@router.get(
    "/{token}.{ext}",
    response_class=Response,
    responses={
        200: {"content": {"image/*": {}}, "description": "The image bytes."},
        304: {"description": "Not modified (matching If-None-Match)."},
        404: {"description": "Not a link this service issued."},
        410: {"description": "The link has expired, or was revoked (or its image deleted)."},
        503: {
            "description": "Storage unavailable, or too many conversions in progress; retry "
            "after `Retry-After`."
        },
    },
)
def get_shared_image(
    token: str,
    ext: str,
    request: Request,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
    cache: VariantCacheDep,
    signer: ShareSignerDep,
    conversion_slots: ShareConversionSlotsDep,
) -> Response:
    """Download an image through a share link (POST /images/{id}/share-links), in the format and
    quality the link was created for. The query string is ignored."""
    # Checked without any I/O, so made-up links cost next to nothing.
    link = signer.verify(token)
    if link is None or ext != FORMATS[link.format].extension:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Share link not found", headers=_ERROR_HEADERS
        )
    now = signer.now()
    if now >= link.expires:
        expired_at = datetime.fromtimestamp(link.expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raise HTTPException(
            status.HTTP_410_GONE, f"Share link expired at {expired_at}", headers=_ERROR_HEADERS
        )
    image = db.get(Image, link.image_id)
    # A deleted image looks like a revoked link, so a link never tells whether its image exists.
    if image is None or image.share_generation != link.generation:
        raise HTTPException(
            status.HTTP_410_GONE, "Share link has been revoked", headers=_ERROR_HEADERS
        )
    headers = {
        "Cache-Control": f"private, max-age={min(int(link.expires - now), MAX_AGE_SECONDS)}",
        # Never the uploader's file name, nor the token.
        "Content-Disposition": f'inline; filename="image.{ext}"',
        # Embeddable by pages that require it (Cross-Origin-Embedder-Policy: require-corp).
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Robots-Tag": "noindex",
        **_CROSS_ORIGIN,
    }
    return send_image(
        request,
        image,
        link.format,
        link.quality,
        headers=headers,
        db=db,
        storage=storage,
        settings=settings,
        cache=cache,
        conversion_slots=conversion_slots,
    )
