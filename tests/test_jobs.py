import signal
import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError

import app.jobs
from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs import RETRY_BASE_SECONDS, _retry_later, claim_next, fail, prune_finished, run_job
from app.main import app as api
from app.models import Image, Job
from app.storage import StorageUnavailableError
from app.worker import MAX_ERROR_BACKOFF_SECONDS, stop_on_signals
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
        ('respond-async; note="a, b"', 202),
        ('note="a, \\"b\\"", respond-async', 202),
        ("return=minimal", 201),
        ("respond-asynchronously", 201),
        # Quoted values are opaque, even when they contain commas or semicolons.
        ('foo="respond-async"', 201),
        ('foo="a, respond-async, b"', 201),
        ('wait=5; note="x, respond-async;y"', 201),
        ('foo="unterminated, respond-async', 201),
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


def test_deleting_the_source_while_the_job_runs_fails_it(client, auth_headers, worker, storage):
    job = queued(client, auth_headers)
    real_save = storage.save

    def save_then_delete_the_source(key, data, *, content_type):
        real_save(key, data, content_type=content_type)
        response = client.delete(f"/images/{job['source_image_id']}", headers=auth_headers)
        assert response.status_code == 204

    storage.save = save_then_delete_the_source

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"] == {"status_code": 404, "detail": "Image not found"}
    assert stored_keys(storage) == []  # the result file was discarded


def test_deleting_the_source_before_its_file_is_read_fails_the_job(
    client, auth_headers, worker, storage
):
    job = queued(client, auth_headers)
    real_read = storage.read

    def delete_the_source_then_read(key):
        response = client.delete(f"/images/{job['source_image_id']}", headers=auth_headers)
        assert response.status_code == 204
        return real_read(key)

    storage.read = delete_the_source_then_read

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "failed"
    assert done["error"] == {"status_code": 404, "detail": "Image not found"}


def test_deleting_the_source_while_its_job_completes_does_not_deadlock(
    client, auth_headers, worker, storage
):
    """Completing a job and deleting its source lock the same two rows (the job and the source
    image); they must do so in the same order. The source is deleted right after the first
    statement of the completing transaction that comes after its storage quota check (which locks
    the owner's row), while that transaction is still open."""
    job = queued(client, auth_headers)
    engine = get_sessionmaker().kw["bind"]
    worker_thread = threading.get_ident()
    completing = threading.Event()
    outcome = {}

    def delete_source():
        response = client.delete(f"/images/{job['source_image_id']}", headers=auth_headers)
        outcome["delete"] = response.status_code

    deleter = threading.Thread(target=delete_source)
    real_complete = app.jobs._complete

    def complete(*args):
        completing.set()
        return real_complete(*args)

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if threading.get_ident() != worker_thread or not completing.is_set():
            return
        if "FROM users" in statement or "sum(images.size_bytes)" in statement:
            return  # The quota check.
        completing.clear()
        deleter.start()
        # Until the delete waits for a lock this transaction holds (or has finished).
        deadline = time.monotonic() + 5
        while deleter.is_alive() and time.monotonic() < deadline:
            with engine.connect() as probe:
                waiting = probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            if waiting:
                break
            time.sleep(0.01)

    app.jobs._complete = complete
    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        worker.run_once()
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
        app.jobs._complete = real_complete
        if deleter.ident:
            deleter.join(10)

    assert outcome == {"delete": 204}
    done = client.get(job["url"], headers=auth_headers).json()
    assert done["status"] == "succeeded"
    assert done["source_image_id"] is None
    assert done["result"]["parent_id"] is None


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


def test_database_errors_during_a_job_leave_it_to_be_retried(
    client, auth_headers, worker, monkeypatch
):
    job = queued(client, auth_headers)

    def database_went_away(*args):
        raise OperationalError("INSERT", {}, Exception("database went away"))

    monkeypatch.setattr(app.jobs, "_complete", database_went_away)

    with pytest.raises(OperationalError):  # for the worker loop to back off
        worker.run_once()

    # Not failed: it runs again once its lease is over.
    assert db_job(job["id"]).status == "running"


def test_a_job_that_succeeds_after_a_retry_has_no_error(client, auth_headers, worker, storage):
    job = queued(client, auth_headers)
    real_read = storage.read

    def unavailable(key):
        raise StorageUnavailableError("down")

    storage.read = unavailable
    worker.run_once()
    assert client.get(job["url"], headers=auth_headers).json()["error"]["status_code"] == 503
    storage.read = real_read
    update_job(job["id"], available_at=func.now())

    worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert (done["status"], done["attempts"], done["error"]) == ("succeeded", 2, None)


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


def test_a_claim_leases_the_job(client, auth_headers):
    job = queued(client, auth_headers)

    with get_sessionmaker()() as db:
        claim = claim_next(db, 300)

    claimed = db_job(job["id"])
    assert (claimed.status, claimed.attempts, claimed.claim_token) == ("running", 1, claim.token)
    assert 298 < seconds_until(claimed.available_at) <= 300
    with get_sessionmaker()() as db:
        assert claim_next(db, 300) is None  # leased


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


def test_a_stale_run_cannot_fail_or_requeue_a_job_taken_over(client, auth_headers, storage):
    job = queued(client, auth_headers)
    sessions, settings = get_sessionmaker(), get_settings()
    with sessions() as db:
        stale = claim_next(db, 300)
    update_job(job["id"], available_at=func.now() - timedelta(seconds=1))
    with sessions() as db:
        current = claim_next(db, 300)

    fail(sessions, stale, 500, "stale")
    _retry_later(sessions, settings, stale, StorageUnavailableError("down"))

    after = db_job(job["id"])
    assert (after.status, after.claim_token, after.error_status) == ("running", current.token, None)
    assert seconds_until(after.available_at) > 290


def test_a_run_whose_lease_ran_out_cannot_finish(client, auth_headers, storage):
    """Even if no other worker has taken the job over yet: its result would be committed after
    the lease, which the orphan sweep's grace period doesn't allow for."""
    job = queued(client, auth_headers)
    sessions, settings = get_sessionmaker(), get_settings()
    with sessions() as db:
        late = claim_next(db, 300)
    update_job(job["id"], available_at=func.now() - timedelta(seconds=1))

    run_job(sessions, storage, settings, late)
    fail(sessions, late, 500, "late")

    after = db_job(job["id"])
    assert (after.status, after.claim_token, after.error_status) == ("running", late.token, None)
    assert len(stored_keys(storage)) == 1  # its result file was discarded
    with sessions() as db:
        assert claim_next(db, 300).attempts == 2  # the job is run again


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


class RecordingStop:
    """Stands in for the worker's stop event: records how long each wait() was asked to last,
    returns at once, and is set after `waits` of them (or, so that a loop that stopped waiting
    fails instead of spinning forever, after many more checks)."""

    def __init__(self, waits: int) -> None:
        self.waits: list[float] = []
        self._limit = waits
        self._checks = 0

    def is_set(self) -> bool:
        self._checks += 1
        return len(self.waits) >= self._limit or self._checks > 10 * self._limit

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.is_set()


def test_worker_loop_prunes_first_and_waits_between_polls_when_idle(worker, monkeypatch):
    pruned = []
    monkeypatch.setattr(worker, "prune", lambda: pruned.append(1))
    monkeypatch.setattr(worker, "run_once", lambda: False)
    stop = RecordingStop(waits=3)

    worker.run(stop)

    assert pruned == [1]  # at startup; the next one is due in an hour
    assert stop.waits == [worker._settings.job_poll_seconds] * 3


def test_worker_loop_backs_off_on_database_errors_and_resets(worker, monkeypatch):
    monkeypatch.setattr(worker, "prune", lambda: None)
    outcomes = iter([OperationalError, OperationalError, False, OperationalError])

    def run_once():
        outcome = next(outcomes, OperationalError)
        if outcome is OperationalError:
            raise OperationalError("SELECT 1", {}, Exception("database went away"))
        return outcome

    monkeypatch.setattr(worker, "run_once", run_once)
    stop = RecordingStop(waits=7)

    worker.run(stop)

    poll = worker._settings.job_poll_seconds
    assert stop.waits == [2 * poll, 4 * poll, poll, 2 * poll, 4 * poll, 8 * poll, 16 * poll]


@pytest.mark.parametrize(
    ("table", "column"), [("images", "share_generation"), ("users", "storage_quota_bytes")]
)
def test_worker_waits_until_the_database_is_fully_migrated(worker, table, column):
    """Not just until the jobs table exists: until every column the worker's code maps does."""
    engine = get_sessionmaker().kw["bind"]
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {column} TO not_yet"))
    try:
        stop = RecordingStop(waits=3)
        assert worker._wait_for_database(stop) is False
        assert len(stop.waits) == 3
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN not_yet TO {column}"))

    assert worker._wait_for_database(RecordingStop(waits=3)) is True


def test_worker_loop_backoff_stays_capped_through_a_long_outage(worker, monkeypatch):
    def run_once():
        raise OperationalError("SELECT 1", {}, Exception("database went away"))

    monkeypatch.setattr(worker, "prune", lambda: None)
    monkeypatch.setattr(worker, "run_once", run_once)
    stop = RecordingStop(waits=3000)

    worker.run(stop)

    assert stop.waits[-1] == MAX_ERROR_BACKOFF_SECONDS


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signals_set_the_stop_event_from_another_thread(monkeypatch, signum):
    """A handler runs on the main thread, possibly while stop.wait() there holds the lock that
    stop.set() takes: setting it from the handler itself could deadlock."""
    handlers = {}
    monkeypatch.setattr(signal, "signal", handlers.__setitem__)

    class RecordingEvent(threading.Event):
        def set(self) -> None:
            self.setter = threading.get_ident()
            super().set()

    stop = RecordingEvent()
    stop_on_signals(stop)

    handlers[signum](signum, None)

    assert stop.wait(5)
    assert stop.setter != threading.get_ident()


def test_openapi_documents_async_transforms_and_jobs():
    paths = api.openapi()["paths"]
    accepted = paths["/images/{image_id}/transform"]["post"]["responses"]["202"]

    assert accepted["content"]["application/json"]["schema"]["$ref"].endswith("/JobOut")
    assert set(accepted["headers"]) == {"Location", "Preference-Applied"}
    assert "get" in paths["/jobs/{job_id}"]
