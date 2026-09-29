import math
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.deps import CurrentUser, DbSession, SettingsDep, StorageDep
from app.imaging import FORMATS, apply_transformations, load_image
from app.models import Image, User
from app.schemas import ImageFormat, ImageList, ImageOut, TransformationSpec, TransformRequest
from app.storage import LocalStorage, build_key

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
    format: Annotated[
        ImageFormat | None, Query(description="Convert to this format on the fly.")
    ] = None,
    quality: Annotated[int | None, Query(ge=1, le=100, description="JPEG/WebP quality.")] = None,
) -> Response:
    """Download an image, optionally converted to another format or quality."""
    image = _get_owned_image(db, user, image_id)
    serve_original = quality is None and format in (None, image.format)

    etag = f'"{image.id.hex}"' if serve_original else f'"{image.id.hex}-{format}-{quality}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    if serve_original:
        return FileResponse(
            storage.path(image.storage_key), media_type=image.mime_type, headers=headers
        )

    loaded = load_image(storage.read(image.storage_key), settings.max_image_pixels)
    result = apply_transformations(
        loaded,
        TransformationSpec(format=format or image.format, quality=quality),
        settings.max_dimension,
    )
    return Response(result.data, media_type=result.mime_type, headers=headers)


@router.post("/{image_id}/transform", status_code=status.HTTP_201_CREATED)
def transform_image(
    image_id: uuid.UUID,
    body: TransformRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> ImageOut:
    """Apply transformations to an image and save the result as a new image.

    The source image is left untouched; the new image records it as its `parent_id`.
    """
    source = _get_owned_image(db, user, image_id)
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
    image_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: StorageDep
) -> None:
    """Delete an image. Images transformed from it are kept."""
    image = _get_owned_image(db, user, image_id)
    storage_key = image.storage_key
    db.delete(image)
    db.commit()
    storage.delete(storage_key)


def _get_owned_image(db: DbSession, user: User, image_id: uuid.UUID) -> Image:
    image = db.get(Image, image_id)
    # Other users' images are reported as missing so their ids can't be probed.
    if image is None or image.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    return image


def _persist(db: DbSession, storage: LocalStorage, image: Image, data: bytes) -> None:
    storage.save(image.storage_key, data)
    db.add(image)
    try:
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(image.storage_key)
        raise


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
