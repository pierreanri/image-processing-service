import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs import RETRY_BASE_SECONDS, claim_next, prune_finished, run_job
from app.main import app
from app.models import Image, Job
from app.storage import StorageUnavailableError
from tests.utils import ALLOWED, REJECTED, make_image_bytes, register, stored_keys, user_id_of


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


def upload(client, headers, size=(100, 50)) -> dict:
    files = {"file": ("a.png", make_image_bytes(size=size), "image/png")}
    response = client.post("/images", headers=headers, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, headers, image_id, transformations=None, prefer="respond-async"):
    return client.post(
        f"/images/{image_id}/transform",
        headers={**headers, "Prefer": prefer},
        json={"transformations": transformations or {"flip": True}},
    )


def queued(client, headers, **kwargs) -> dict:
    image = upload(client, headers)
    response = submit(client, headers, image["id"], **kwargs)
    assert response.status_code == 202, response.text
    return response.json()


def db_jobs() -> list[Job]:
    with get_sessionmaker()() as db:
        return list(db.scalars(select(Job).order_by(Job.created_at)))


def db_job(job_id) -> Job:
    with get_sessionmaker()() as db:
        return db.get(Job, uuid.UUID(str(job_id)))


def update_job(job_id, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(update(Job).where(Job.id == uuid.UUID(str(job_id))).values(**values))
        db.commit()


def seconds_until(moment) -> float:
    with get_sessionmaker()() as db:
        return (moment - db.scalar(select(func.now()))).total_seconds()


# --- Submitting -----------------------------------------------------------------------------------


def test_async_transform_is_accepted_as_a_queued_job(client, auth_headers, rate_limiter):
    rate_limiter.decision = ALLOWED
    image = upload(client, auth_headers)

    response = submit(client, auth_headers, image["id"])

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert job["source_image_id"] == image["id"]
    assert job["transformations"] == {"flip": True}
    assert (job["attempts"], job["result"], job["error"]) == (0, None, None)
    assert response.headers["location"] == job["url"] == f"http://testserver/jobs/{job['id']}"
    assert response.headers["preference-applied"] == "respond-async"
    assert response.headers["ratelimit"] == ALLOWED.headers()["RateLimit"]
    assert rate_limiter.hits == [user_id_of(auth_headers)]
    # Nothing is created until a worker runs the job.
    assert client.get("/images", headers=auth_headers).json()["total"] == 1

    polled = client.get(job["url"], headers=auth_headers)
    assert polled.status_code == 200
    assert polled.json()["status"] == "queued"
    assert polled.headers["retry-after"] == "1"


@pytest.mark.parametrize(
    ("prefer", "expected_status"),
    [
        ("respond-async", 202),
        ("RESPOND-ASYNC", 202),
        ("respond-async, wait=5", 202),
        ("wait=5, respond-async", 202),
        ("respond-async; foo=bar", 202),
        ("return=minimal", 201),
        ("respond-asynchronously", 201),
    ],
)
def test_prefer_header_is_parsed(client, auth_headers, prefer, expected_status):
    image = upload(client, auth_headers)

    response = submit(client, auth_headers, image["id"], prefer=prefer)

    assert response.status_code == expected_status
    assert len(db_jobs()) == (1 if expected_status == 202 else 0)


def test_respond_async_in_a_second_prefer_header(client, auth_headers):
    image = upload(client, auth_headers)
    headers = [*auth_headers.items(), ("Prefer", "wait=5"), ("Prefer", "respond-async")]

    response = client.post(
        f"/images/{image['id']}/transform",
        headers=headers,
        json={"transformations": {"flip": True}},
    )

    assert response.status_code == 202


def test_transform_without_prefer_stays_synchronous(client, auth_headers):
    image = upload(client, auth_headers)

    response = client.post(
        f"/images/{image['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"flip": True}},
    )

    assert response.status_code == 201
    assert response.json()["parent_id"] == image["id"]
    assert "preference-applied" not in response.headers
    assert db_jobs() == []


def test_rate_limited_async_request_creates_no_job(client, auth_headers, rate_limiter):
    image = upload(client, auth_headers)
    rate_limiter.decision = REJECTED

    response = submit(client, auth_headers, image["id"])

    assert response.status_code == 429
    assert db_jobs() == []


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [("no token", 401), ("invalid body", 422), ("unknown image", 404), ("other user", 404)],
)
def test_async_requests_are_checked_before_queueing(client, auth_headers, case, expected_status):
    image = upload(client, auth_headers)
    headers, image_id, transformations = auth_headers, image["id"], {"flip": True}
    if case == "no token":
        headers = {}
    elif case == "invalid body":
        transformations = {"rotate": 720}
    elif case == "unknown image":
        image_id = uuid.uuid4()
    else:
        headers = register(client, "bob")

    response = client.post(
        f"/images/{image_id}/transform",
        headers={**headers, "Prefer": "respond-async"},
        json={"transformations": transformations},
    )

    assert response.status_code == expected_status
    assert db_jobs() == []


def test_jobs_are_private(client, auth_headers):
    job = queued(client, auth_headers)
    bob = register(client, "bob")

    assert client.get(job["url"], headers=bob).status_code == 404
    assert client.get(job["url"]).status_code == 401
    assert client.get(f"/jobs/{uuid.uuid4()}", headers=auth_headers).status_code == 404


# --- Running --------------------------------------------------------------------------------------


def test_worker_runs_the_job_and_the_result_downloads(client, auth_headers, worker, storage):
    image = upload(client, auth_headers, size=(100, 50))
    spec = {"resize": {"width": 40}, "format": "webp"}
    job = submit(client, auth_headers, image["id"], transformations=spec).json()

    assert worker.run_once() is True
    assert worker.run_once() is False

    polled = client.get(job["url"], headers=auth_headers)
    done = polled.json()
    assert done["status"] == "succeeded"
    assert done["attempts"] == 1
    assert done["started_at"] and done["finished_at"]
    assert done["error"] is None
    assert "retry-after" not in polled.headers
    result = done["result"]
    assert result["parent_id"] == image["id"]
    assert (result["width"], result["height"], result["format"]) == (40, 20, "webp")
    assert result["transformations"] == spec
    content = client.get(result["url"], headers=auth_headers)
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/webp"
    assert client.get("/images", headers=auth_headers).json()["total"] == 2
    assert len(stored_keys(storage)) == 2


def test_a_failed_transformation_fails_the_job(client, auth_headers, worker, storage):
    job = queued(
        client, auth_headers, transformations={"crop": {"x": 90, "width": 50, "height": 5}}
    )

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"]["status_code"] == 422
    assert "outside" in done["error"]["detail"]
    assert done["result"] is None
    assert len(stored_keys(storage)) == 1


def test_deleting_the_source_first_fails_the_job(client, auth_headers, worker):
    job = queued(client, auth_headers)
    client.delete(f"/images/{job['source_image_id']}", headers=auth_headers)

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"] == {"status_code": 404, "detail": "Image not found"}
    assert done["source_image_id"] is None


def test_storage_outages_are_retried_with_backoff_then_fail(client, auth_headers, worker, storage):
    job = queued(client, auth_headers)

    def unavailable(key):
        raise StorageUnavailableError("down")

    storage.read = unavailable

    for attempt in (1, 2):
        worker.run_once()
        waiting = db_job(job["id"])
        assert (waiting.status, waiting.attempts) == ("queued", attempt)
        delay = RETRY_BASE_SECONDS * 2 ** (attempt - 1)
        assert delay - 2 < seconds_until(waiting.available_at) <= delay
        polled = client.get(job["url"], headers=auth_headers).json()
        assert polled["error"]["status_code"] == 503
        assert worker.run_once() is False  # not due yet
        update_job(job["id"], available_at=func.now())

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert (done["status"], done["attempts"]) == ("failed", 3)
    assert done["error"] == {
        "status_code": 503,
        "detail": "Image storage is temporarily unavailable",
    }


def test_unexpected_errors_fail_the_job(client, auth_headers, worker, storage):
    job = queued(client, auth_headers)

    def broken(key):
        raise RuntimeError("bug")

    storage.read = broken

    assert worker.run_once() is True

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"] == {
        "status_code": 500,
        "detail": "Internal error while processing the job",
    }


# --- Claims and leases ----------------------------------------------------------------------------


def test_claims_skip_jobs_locked_by_another_worker(client, auth_headers):
    first = queued(client, auth_headers)
    second = queued(client, auth_headers)
    sessions = get_sessionmaker()

    with sessions() as holder:
        holder.execute(select(Job).where(Job.id == uuid.UUID(first["id"])).with_for_update())
        with sessions() as db:
            claim = claim_next(db, 300)
        assert claim.job_id == uuid.UUID(second["id"])
        with sessions() as db:
            assert claim_next(db, 300) is None

    with sessions() as db:
        assert claim_next(db, 300).job_id == uuid.UUID(first["id"])


def test_an_expired_lease_is_taken_over_and_the_stale_run_cannot_finish(
    client, auth_headers, storage
):
    job = queued(client, auth_headers)
    sessions, settings = get_sessionmaker(), get_settings()
    with sessions() as db:
        stale = claim_next(db, 300)
    update_job(job["id"], available_at=func.now() - timedelta(seconds=1))  # lease expired
    with sessions() as db:
        current = claim_next(db, 300)
    assert (current.job_id, current.attempts) == (stale.job_id, 2)
    assert current.token != stale.token

    run_job(sessions, storage, settings, stale)

    assert db_job(job["id"]).status == "running"
    assert len(stored_keys(storage)) == 1  # the stale run's file was discarded

    run_job(sessions, storage, settings, current)

    assert db_job(job["id"]).status == "succeeded"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Image)) == 2
    assert len(stored_keys(storage)) == 2


def test_jobs_interrupted_too_often_are_failed(client, auth_headers, worker):
    job = queued(client, auth_headers)
    # As if three workers had crashed while running it.
    update_job(job["id"], status="running", attempts=3, available_at=func.now())

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"] == {"status_code": 500, "detail": "The job was interrupted too many times"}


def test_finished_jobs_are_pruned_after_the_retention_period(client, auth_headers, worker):
    old_done, recent_done, waiting = (queued(client, auth_headers) for _ in range(3))
    worker.run_once()
    worker.run_once()
    update_job(old_done["id"], finished_at=func.now() - timedelta(days=8))
    update_job(waiting["id"], created_at=func.now() - timedelta(days=30))

    with get_sessionmaker()() as db:
        assert prune_finished(db, 7) == 1

    remaining = {str(job.id) for job in db_jobs()}
    assert remaining == {recent_done["id"], waiting["id"]}


# --- The worker loop ------------------------------------------------------------------------------


def test_worker_loop_runs_jobs_until_stopped(client, auth_headers, worker):
    job = queued(client, auth_headers)
    worker._settings = get_settings().model_copy(update={"job_poll_seconds": 0.05})
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,))

    thread.start()
    try:
        deadline = time.monotonic() + 10
        while db_job(job["id"]).status != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(5)

    assert db_job(job["id"]).status == "succeeded"
    assert not thread.is_alive()


def test_worker_loop_survives_database_errors(worker, monkeypatch):
    worker._settings = get_settings().model_copy(update={"job_poll_seconds": 0.01})
    stop = threading.Event()
    calls = []

    def flaky_run_once():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("database went away"))
        stop.set()
        return False

    monkeypatch.setattr(worker, "run_once", flaky_run_once)

    worker.run(stop)

    assert len(calls) == 2


def test_openapi_documents_async_transforms_and_jobs():
    paths = app.openapi()["paths"]
    accepted = paths["/images/{image_id}/transform"]["post"]["responses"]["202"]

    assert accepted["content"]["application/json"]["schema"]["$ref"].endswith("/JobOut")
    assert set(accepted["headers"]) == {"Location", "Preference-Applied"}
    assert "get" in paths["/jobs/{job_id}"]
