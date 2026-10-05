"""Response bodies shared by the image and job routers, and webhook payloads."""

from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import Request
from starlette.applications import Starlette
from starlette.datastructures import URL

from app.imaging import FORMATS
from app.models import Image, Job, WebhookDelivery
from app.schemas import ImageOut, JobData, JobError, JobOut, ShareLinkOut, WebhookDeliveryOut
from app.sharing import ShareLink


class UrlSource(Protocol):
    """Builds the URL of a route by its name: a Request, or PublicUrls."""

    def url_for(self, name: str, /, **path_params: Any) -> Any: ...


class PublicUrls:
    """The API's URLs under PUBLIC_BASE_URL, for workers, which have no request to take them
    from. A request builds the same URLs when PUBLIC_BASE_URL is the URL clients use."""

    def __init__(self, app: Starlette, base_url: str) -> None:
        self._app = app
        self._base_url = base_url

    def url_for(self, name: str, /, **path_params: Any) -> URL:
        return self._app.url_path_for(name, **path_params).make_absolute_url(self._base_url)


def image_out(image: Image, urls: UrlSource) -> ImageOut:
    return ImageOut(
        id=image.id,
        parent_id=image.parent_id,
        url=str(urls.url_for("get_image_content", image_id=image.id)),
        original_filename=image.original_filename,
        format=image.format,
        mime_type=image.mime_type,
        width=image.width,
        height=image.height,
        size_bytes=image.size_bytes,
        transformations=image.transformations,
        created_at=image.created_at,
    )


def job_out(
    job: Job, urls: UrlSource, result: Image | None, delivery: WebhookDelivery | None = None
) -> JobOut:
    return JobOut(
        **dict(job_data(job, urls, result)),
        webhook=delivery_out(delivery) if delivery is not None else None,
    )


def job_data(job: Job, urls: UrlSource, result: Image | None) -> JobData:
    error = None
    if job.error_status is not None:
        error = JobError(status_code=job.error_status, detail=job.error_detail or "")
    return JobData(
        id=job.id,
        url=str(urls.url_for("get_job", job_id=job.id)),
        status=job.status,
        source_image_id=job.source_image_id,
        transformations=job.transformations,
        attempts=job.attempts,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=image_out(result, urls) if result is not None else None,
        error=error,
    )


def delivery_out(delivery: WebhookDelivery) -> WebhookDeliveryOut:
    return WebhookDeliveryOut(
        event_id=delivery.id,
        type=delivery.type,
        state=delivery.state,
        attempts=delivery.attempts,
        last_attempt_at=delivery.last_attempt_at,
        last_status_code=delivery.last_status_code,
        last_error=delivery.last_error,
        next_attempt_at=delivery.next_attempt_at if delivery.state == "pending" else None,
        finished_at=delivery.finished_at,
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
