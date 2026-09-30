"""Background transformations, queued in the `jobs` table and run by app/worker.py.

A job goes queued -> running -> succeeded or failed. Workers claim jobs with
FOR UPDATE SKIP LOCKED, so any number of them can share the table. A claim is a lease: a job
whose worker died is taken over once `available_at` (the lease deadline) passes. Each claim
carries a fresh token, and every change a worker makes to a job requires that token, so a
worker whose lease expired can neither finish nor fail a job another worker has taken over.
Processing happens outside any transaction: the result file is saved first, then one
transaction inserts the result image and marks the job succeeded.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.imaging import ImageProcessingError
from app.models import Image, Job
from app.schemas import TransformationSpec
from app.storage import Storage, StorageUnavailableError
from app.transforms import discard_file, render_transformation

logger = logging.getLogger(__name__)

# A job whose storage was unavailable is retried after 10 s, then 20 s, 40 s, ...
RETRY_BASE_SECONDS = 10
STORAGE_UNAVAILABLE = "Image storage is temporarily unavailable"
SOURCE_GONE = "Image not found"


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    token: uuid.UUID
    # Including this run.
    attempts: int


def enqueue(
    db: Session, owner_id: uuid.UUID, source_id: uuid.UUID, spec: TransformationSpec
) -> Job:
    job = Job(
        owner_id=owner_id,
        source_image_id=source_id,
        transformations=spec.model_dump(mode="json", exclude_defaults=True),
        status="queued",
        attempts=0,
    )
    db.add(job)
    db.commit()
    return job


def claim_next(db: Session, lease_seconds: int) -> Claim | None:
    """Claim the job that has been waiting longest (or whose previous worker's lease ran out),
    in one short transaction. Returns None when there is nothing to do."""
    token = uuid.uuid4()
    candidate = (
        select(Job.id)
        .where(Job.status.in_(("queued", "running")), Job.available_at <= func.now())
        .order_by(Job.available_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    claimed = db.execute(
        update(Job)
        .where(Job.id == candidate)
        .values(
            status="running",
            attempts=Job.attempts + 1,
            claim_token=token,
            started_at=func.coalesce(Job.started_at, func.now()),
            available_at=func.now() + timedelta(seconds=lease_seconds),
        )
        .returning(Job.id, Job.attempts)
        .execution_options(synchronize_session=False)
    ).one_or_none()
    db.commit()
    return Claim(claimed.id, token, claimed.attempts) if claimed else None


def run_job(
    sessions: sessionmaker[Session], storage: Storage, settings: Settings, claim: Claim
) -> None:
    """Run a claimed job to completion (or back into the queue after a storage outage)."""
    if claim.attempts > settings.job_max_attempts:
        # Only runs that were interrupted (a crashed worker) get here: the others end below.
        fail(sessions, claim, 500, "The job was interrupted too many times")
        return

    with sessions() as db:
        job = db.get(Job, claim.job_id)
        if job is None:
            return
        owner_id = job.owner_id
        spec = TransformationSpec.model_validate(job.transformations)
        source = db.get(Image, job.source_image_id) if job.source_image_id else None
        db.commit()
    if source is None:
        fail(sessions, claim, 404, SOURCE_GONE)
        return

    try:
        image, data = render_transformation(storage, settings, source, spec, owner_id)
        storage.save(image.storage_key, data, content_type=image.mime_type)
    except ImageProcessingError as exc:
        fail(sessions, claim, exc.status_code, str(exc))
        return
    except StorageUnavailableError as exc:
        _retry_later(sessions, settings, claim, exc)
        return

    if not _complete(sessions, claim, image):
        discard_file(storage, image.storage_key)


def _complete(sessions: sessionmaker[Session], claim: Claim, image: Image) -> bool:
    """Insert the result image and mark the job succeeded, if this claim still holds it."""
    with sessions() as db:
        job = db.scalar(_claimed(claim).with_for_update())
        if job is None:
            logger.info("Job %s was taken over or deleted; discarding its result", claim.job_id)
            return False
        db.add(image)
        try:
            db.flush()
        except IntegrityError:
            # The source image (the result's parent) was deleted in the meantime.
            db.rollback()
            fail(sessions, claim, 404, SOURCE_GONE)
            return False
        job.status = "succeeded"
        job.result_image_id = image.id
        job.finished_at = func.now()
        job.claim_token = None
        job.error_status = job.error_detail = None
        db.commit()
    logger.info("Job %s succeeded: image %s", claim.job_id, image.id)
    return True


def fail(sessions: sessionmaker[Session], claim: Claim, status_code: int, detail: str) -> None:
    """Mark a job failed, if this claim still holds it."""
    with sessions() as db:
        updated = db.execute(
            _update_claimed(claim).values(
                status="failed",
                error_status=status_code,
                error_detail=detail,
                finished_at=func.now(),
                claim_token=None,
            )
        ).rowcount
        db.commit()
    if updated:
        logger.info("Job %s failed (%s): %s", claim.job_id, status_code, detail)


def _retry_later(
    sessions: sessionmaker[Session], settings: Settings, claim: Claim, exc: Exception
) -> None:
    if claim.attempts >= settings.job_max_attempts:
        fail(sessions, claim, 503, STORAGE_UNAVAILABLE)
        return
    delay = RETRY_BASE_SECONDS * 2 ** (claim.attempts - 1)
    with sessions() as db:
        db.execute(
            _update_claimed(claim).values(
                status="queued",
                available_at=func.now() + timedelta(seconds=delay),
                error_status=503,
                error_detail=STORAGE_UNAVAILABLE,
                claim_token=None,
            )
        )
        db.commit()
    logger.warning("Job %s will be retried in %ss: %s", claim.job_id, delay, exc)


def prune_finished(db: Session, retention_days: int) -> int:
    """Delete jobs that finished more than `retention_days` ago; returns how many."""
    deleted = db.execute(
        delete(Job)
        .where(
            Job.status.in_(("succeeded", "failed")),
            Job.finished_at < func.now() - timedelta(days=retention_days),
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return deleted


def _claimed(claim: Claim):
    return select(Job).where(
        Job.id == claim.job_id, Job.claim_token == claim.token, Job.status == "running"
    )


def _update_claimed(claim: Claim):
    return (
        update(Job)
        .where(Job.id == claim.job_id, Job.claim_token == claim.token, Job.status == "running")
        .execution_options(synchronize_session=False)
    )
