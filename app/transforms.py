"""Creating a transformed image, shared by POST /images/{id}/transform and the job worker."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import Settings
from app.imaging import apply_transformations, load_image
from app.models import Image
from app.quota import check_quota
from app.schemas import TransformationSpec
from app.storage import Storage, build_key

logger = logging.getLogger(__name__)


def render_transformation(
    storage: Storage,
    settings: Settings,
    source: Image,
    spec: TransformationSpec,
    owner_id: uuid.UUID,
) -> tuple[Image, bytes]:
    """Transform `source` and return the new (not yet stored or saved) image and its bytes.

    Raises ImageProcessingError when the transformation doesn't fit the image, and
    StorageUnavailableError when the source can't be read.
    """
    loaded = load_image(storage.read(source.storage_key), settings.max_image_pixels)
    result = apply_transformations(loaded, spec, settings.max_dimension)
    image = Image(
        id=uuid.uuid4(),
        owner_id=owner_id,
        parent_id=source.id,
        storage_key=build_key(owner_id, result.extension),
        original_filename=with_extension(source.original_filename, result.extension),
        format=result.format,
        mime_type=result.mime_type,
        width=result.width,
        height=result.height,
        size_bytes=len(result.data),
        transformations=spec.model_dump(mode="json", exclude_defaults=True),
    )
    return image, result.data


def release_connection(db: Session) -> None:
    """End the session's read-only transaction so its pooled connection isn't held while
    storage (possibly S3, over the network) is slow: the DB pool is smaller than the
    threadpool, and running out would break endpoints that never touch storage. Loaded
    objects stay usable (expire_on_commit=False) and a later query checks out a connection
    again."""
    db.commit()


def persist_image(
    db: Session, storage: Storage, settings: Settings, image: Image, data: bytes
) -> None:
    """Store an image's file, then its row, within its owner's storage quota (raises
    QuotaExceededError otherwise)."""
    # Refuses what certainly doesn't fit before storing anything.
    check_quota(db, settings, image.owner_id, image.size_bytes)
    release_connection(db)
    # File first: a crash in between leaves an orphaned file, never a row without its file.
    storage.save(image.storage_key, data, content_type=image.mime_type)
    try:
        # The check that decides: first in the transaction that inserts the row.
        check_quota(db, settings, image.owner_id, image.size_bytes, lock=True)
        db.add(image)
        db.commit()
    except Exception:
        db.rollback()
        discard_file(storage, image.storage_key)
        raise


def discard_file(storage: Storage, key: str) -> None:
    """Delete a file whose image is gone, logging (not raising) a failure."""
    try:
        storage.delete(key)
    except Exception:
        logger.exception("Could not delete image file %s; it is left orphaned", key)


def with_extension(filename: str, extension: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem[: 254 - len(extension)]}.{extension}"
