import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import select, tuple_

from app.deps import CurrentUser, DbSession
from app.models import Image, Job, WebhookDelivery
from app.routers.views import job_out
from app.schemas import JobList, JobOut, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    statuses: Annotated[
        list[JobStatus] | None,
        Query(alias="status", description="Only jobs in these states (repeat to give several)."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    cursor: Annotated[
        str | None, Query(description="The `next_cursor` of the previous page.")
    ] = None,
) -> JobList:
    """Your background transformations, newest (most recently submitted) first.

    Pages are anchored to the last job of the previous page, so jobs submitted while you page
    through don't shift or repeat what you see. Jobs are kept for JOB_RETENTION_DAYS after they
    finish.
    """
    query = (
        select(Job, Image, WebhookDelivery)
        .outerjoin(Image, Image.id == Job.result_image_id)
        .outerjoin(WebhookDelivery, WebhookDelivery.job_id == Job.id)
        .where(Job.owner_id == user.id)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(limit + 1)
    )
    if statuses:
        query = query.where(Job.status.in_(set(statuses)))
    if cursor is not None:
        query = query.where(tuple_(Job.created_at, Job.id) < tuple_(*_decode_cursor(cursor)))
    rows = db.execute(query).all()
    page = rows[:limit]
    return JobList(
        items=[job_out(job, request, result, delivery) for job, result, delivery in page],
        next_cursor=_encode_cursor(page[-1].Job) if len(rows) > limit else None,
    )


@router.get("/{job_id}")
def get_job(
    job_id: uuid.UUID, request: Request, response: Response, user: CurrentUser, db: DbSession
) -> JobOut:
    """A background transformation (see POST /images/{id}/transform with Prefer: respond-async).

    While the job is queued or running, `Retry-After` suggests when to poll again.
    """
    job = db.get(Job, job_id)
    # Other users' jobs are reported as missing so their ids can't be probed.
    if job is None or job.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    result = db.get(Image, job.result_image_id) if job.result_image_id else None
    delivery = db.scalar(select(WebhookDelivery).where(WebhookDelivery.job_id == job.id))
    if job.status in ("queued", "running"):
        response.headers["Retry-After"] = "1"
    return job_out(job, request, result, delivery)


def _encode_cursor(job: Job) -> str:
    """The position just after `job` in the listing's order."""
    position = f"{job.created_at.isoformat()},{job.id}"
    return base64.urlsafe_b64encode(position.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        moment, job_id = base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(",")
        created_at = datetime.fromisoformat(moment)
        if created_at.tzinfo is None:
            raise ValueError("naive time")
        return created_at, uuid.UUID(job_id)
    except (ValueError, binascii.Error):  # UnicodeDecodeError is a ValueError.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from None
