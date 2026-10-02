"""Response bodies shared by the image and job routers."""

from datetime import UTC, datetime

from fastapi import Request

from app.imaging import FORMATS
from app.models import Image, Job
from app.schemas import ImageOut, JobError, JobOut, ShareLinkOut
from app.sharing import ShareLink


def image_out(image: Image, request: Request) -> ImageOut:
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


def job_out(job: Job, request: Request, result: Image | None) -> JobOut:
    error = None
    if job.error_status is not None:
        error = JobError(status_code=job.error_status, detail=job.error_detail or "")
    return JobOut(
        id=job.id,
        url=str(request.url_for("get_job", job_id=job.id)),
        status=job.status,
        source_image_id=job.source_image_id,
        transformations=job.transformations,
        attempts=job.attempts,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=image_out(result, request) if result is not None else None,
        error=error,
    )


def share_link_out(link: ShareLink, token: str, request: Request) -> ShareLinkOut:
    info = FORMATS[link.format]
    return ShareLinkOut(
        url=str(request.url_for("get_shared_image", token=token, ext=info.extension)),
        image_id=link.image_id,
        format=link.format,
        mime_type=info.mime_type,
        quality=link.quality,
        expires_at=datetime.fromtimestamp(link.expires, UTC),
    )
