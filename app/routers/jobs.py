import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.deps import CurrentUser, DbSession
from app.models import Image, Job
from app.routers.views import job_out
from app.schemas import JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


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
    if job.status in ("queued", "running"):
        response.headers["Retry-After"] = "1"
    return job_out(job, request, result)
