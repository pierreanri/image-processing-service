"""Queuing webhook events as jobs finish (app/jobs.py:_finish, app/webhooks.py:queue_job_event).
Sending them is tested in test_webhook_delivery.py."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError

import app.jobs
from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs import claim_next, prune_finished, run_job
from app.models import Image, Job, User, WebhookDelivery, WebhookEndpoint
from app.storage import StorageUnavailableError
from app.webhooks import GAVE_UP, RETRY_FOR, give_up_on_expired_events
from tests.utils import (
    deliveries,
    delivery_of,
    endpoint_of,
    make_image_bytes,
    register,
    seconds_until,
    set_webhook,
    stored_keys,
    update_endpoint,
    user_id_of,
    wait_for_a_lock_wait,
)


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


def queued(client, headers, transformations=None) -> dict:
    files = {"file": ("a.png", make_image_bytes(), "image/png")}
    image = client.post("/images", headers=headers, files=files).json()
    response = client.post(
        f"/images/{image['id']}/transform",
        headers={**headers, "Prefer": "respond-async"},
        json={"transformations": transformations or {"flip": True}},
    )
    assert response.status_code == 202, response.text
    return response.json()


def update_job(job_id, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(Job.__table__.update().where(Job.id == uuid.UUID(str(job_id))).values(**values))
        db.commit()


def db_job(job_id) -> Job:
    with get_sessionmaker()() as db:
        return db.get(Job, uuid.UUID(str(job_id)))


def test_a_succeeded_job_queues_one_event(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    assert deliveries() == []
    assert endpoint_of(user_id).next_attempt_at is None

    assert worker.run_once()

    [delivery] = deliveries()
    finished = db_job(job["id"])
    assert finished.status == "succeeded"
    assert (delivery.job_id, delivery.user_id, delivery.type) == (
        finished.id,
        user_id,
        "job.succeeded",
    )
    assert (delivery.state, delivery.attempts) == ("pending", 0)
    assert delivery.created_at == finished.finished_at
    assert delivery.last_attempt_at is None and delivery.finished_at is None
    # Due now, and so is the endpoint.
    assert -5 < seconds_until(delivery.next_attempt_at) <= 0
    assert endpoint_of(user_id).next_attempt_at == delivery.next_attempt_at

    shown = client.get(job["url"], headers=auth_headers).json()["webhook"]
    assert shown == {
        "event_id": str(delivery.id),
        "type": "job.succeeded",
        "state": "pending",
        "attempts": 0,
        "last_attempt_at": None,
        "last_status_code": None,
        "last_error": None,
        "next_attempt_at": shown["next_attempt_at"],
        "finished_at": None,
    }
    assert shown["next_attempt_at"] is not None
    [listed] = client.get("/jobs", headers=auth_headers).json()["items"]
    assert listed["webhook"] == shown


def test_unfinished_jobs_have_no_webhook_state(client, auth_headers):
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)

    assert job["webhook"] is None
    assert client.get(job["url"], headers=auth_headers).json()["webhook"] is None


def frozen_account(client, headers):
    user_id = user_id_of(headers)
    with get_sessionmaker()() as db:
        db.execute(User.__table__.update().where(User.id == user_id).values(storage_quota_bytes=0))
        db.commit()


def source_deleted(client, headers, job):
    assert client.delete(f"/images/{job['source_image_id']}", headers=headers).status_code == 204


def interrupted_too_often(client, headers, job):
    update_job(job["id"], status="running", attempts=3, available_at=func.now())


@pytest.mark.parametrize(
    ("transformations", "prepare", "error"),
    [
        ({"crop": {"x": 90, "width": 50, "height": 5}}, None, 422),
        (None, source_deleted, 404),
        (None, lambda client, headers, job: frozen_account(client, headers), 403),
        (None, interrupted_too_often, 500),
    ],
    ids=["transformation", "source-deleted", "quota", "interrupted"],
)
def test_each_failure_queues_one_job_failed_event(
    client, auth_headers, worker, transformations, prepare, error
):
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers, transformations)
    if prepare is not None:
        prepare(client, auth_headers, job)

    assert worker.run_once()

    done = client.get(job["url"], headers=auth_headers).json()
    assert (done["status"], done["error"]["status_code"]) == ("failed", error)
    [delivery] = deliveries()
    assert (str(delivery.job_id), delivery.type, delivery.state) == (
        job["id"],
        "job.failed",
        "pending",
    )
    assert done["webhook"]["event_id"] == str(delivery.id)


def test_a_job_that_completes_over_its_quota_queues_a_job_failed_event(
    client, auth_headers, worker, storage, monkeypatch
):
    """The decisive quota check, in the completing transaction, fails it."""
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    real_check = app.jobs.check_quota

    def check(db, settings, owner_id, size, *, lock=False):
        if lock:
            frozen_account(client, auth_headers)
        return real_check(db, settings, owner_id, size, lock=lock)

    monkeypatch.setattr(app.jobs, "check_quota", check)

    assert worker.run_once()

    assert db_job(job["id"]).error_status == 403
    assert [d.type for d in deliveries()] == ["job.failed"]
    assert len(stored_keys(storage)) == 1  # the result file was discarded


def test_unexpected_errors_queue_a_job_failed_event(client, auth_headers, worker, storage):
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)

    def broken(key):
        raise RuntimeError("bug")

    storage.read = broken

    assert worker.run_once()

    assert db_job(job["id"]).error_status == 500
    assert [d.type for d in deliveries()] == ["job.failed"]


def test_storage_retries_queue_an_event_only_when_they_give_up(
    client, auth_headers, worker, storage
):
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)

    def unavailable(key):
        raise StorageUnavailableError("down")

    storage.read = unavailable
    for _ in range(2):
        worker.run_once()
        assert db_job(job["id"]).status == "queued"
        assert deliveries() == []
        update_job(job["id"], available_at=func.now())

    worker.run_once()

    assert db_job(job["id"]).error_status == 503
    assert [d.type for d in deliveries()] == ["job.failed"]


def test_no_event_without_a_webhook_or_for_jobs_finished_before_it_was_set(
    client, auth_headers, worker
):
    before = queued(client, auth_headers)
    worker.run_once()
    bob = register(client, "bob")
    set_webhook(client, bob)
    queued(client, auth_headers)
    worker.run_once()

    set_webhook(client, auth_headers)
    after = queued(client, auth_headers)
    worker.run_once()

    assert [str(d.job_id) for d in deliveries()] == [after["id"]]
    assert client.get(before["url"], headers=auth_headers).json()["webhook"] is None
    assert endpoint_of(user_id_of(bob)).next_attempt_at is None


def test_a_stale_run_queues_no_event(client, auth_headers, storage):
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    sessions, settings = get_sessionmaker(), get_settings()
    with sessions() as db:
        stale = claim_next(db, 300)
    update_job(job["id"], available_at=func.now() - timedelta(seconds=1))
    with sessions() as db:
        current = claim_next(db, 300)

    run_job(sessions, storage, settings, stale)
    app.jobs.fail(sessions, stale, 500, "stale")

    assert deliveries() == []

    run_job(sessions, storage, settings, current)

    assert [d.type for d in deliveries()] == ["job.succeeded"]


def test_the_event_commits_with_the_job_or_not_at_all(client, auth_headers, worker, monkeypatch):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    real_queue = app.jobs.queue_job_event

    def queue_then_fail(db, *args):
        real_queue(db, *args)
        assert db.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
        raise OperationalError("INSERT", {}, Exception("database went away"))

    monkeypatch.setattr(app.jobs, "queue_job_event", queue_then_fail)

    with pytest.raises(OperationalError):
        worker.run_once()

    assert db_job(job["id"]).status == "running"
    assert deliveries() == []
    assert endpoint_of(user_id).next_attempt_at is None
    with get_sessionmaker()() as db:
        assert db.scalar(select(func.count()).select_from(Image)) == 1

    monkeypatch.setattr(app.jobs, "queue_job_event", real_queue)
    update_job(job["id"], available_at=func.now())
    worker.run_once()

    assert db_job(job["id"]).status == "succeeded"
    assert [d.type for d in deliveries()] == ["job.succeeded"]


def finish_while(engine, statement_marker, side, worker_run):
    """Run `worker_run`; once its thread has run a statement containing `statement_marker`, run
    `side` in another thread and let the worker go on once that waits for a lock (or ends)."""
    worker_thread = threading.get_ident()
    other = threading.Thread(target=side)

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        mine = threading.get_ident() == worker_thread
        if mine and statement_marker in statement and not other.ident:
            other.start()
            assert wait_for_a_lock_wait()

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        worker_run()
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
        if other.ident:
            other.join(10)
    return other


def test_removing_the_webhook_while_a_job_completes_cancels_its_event(client, auth_headers, worker):
    """The completing transaction holds the endpoint's lock: the removal waits for it, then
    cancels the event it queued."""
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    engine = get_sessionmaker().kw["bind"]
    removed = []

    other = finish_while(
        engine,
        "UPDATE webhook_endpoints",
        lambda: removed.append(client.delete("/me/webhook", headers=auth_headers).status_code),
        worker.run_once,
    )

    assert other.ident and removed == [204]
    assert db_job(job["id"]).status == "succeeded"
    [delivery] = deliveries()
    assert (delivery.state, delivery.last_error) == ("canceled", "Webhook removed")


def test_a_job_completing_while_the_webhook_is_removed_queues_nothing(client, auth_headers, worker):
    """The removal holds the endpoint's lock: the completing transaction waits for it, then
    finds no webhook."""
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    engine = get_sessionmaker().kw["bind"]
    completed = threading.Thread(target=worker.run_once)
    remover_thread = []

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if "DELETE FROM webhook_endpoints" in statement and not remover_thread:
            remover_thread.append(threading.get_ident())
            completed.start()
            assert wait_for_a_lock_wait()

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        assert client.delete("/me/webhook", headers=auth_headers).status_code == 204
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
        if completed.ident:
            completed.join(10)

    assert remover_thread
    assert db_job(job["id"]).status == "succeeded"
    assert deliveries() == []


def test_an_event_wakes_an_idle_endpoint_at_once(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    update_endpoint(user_id, next_attempt_at=func.now() + timedelta(minutes=30))

    worker.run_once()

    assert -5 < seconds_until(endpoint_of(user_id).next_attempt_at) <= 0


def test_an_event_never_delays_an_endpoint_already_due(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    update_endpoint(user_id, next_attempt_at=func.now() - timedelta(minutes=5))

    worker.run_once()

    assert 290 < -seconds_until(endpoint_of(user_id).next_attempt_at) < 310


def test_events_for_a_paused_endpoint_wait_for_the_pause(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    update_endpoint(user_id, paused_until=func.now() + timedelta(minutes=10))

    worker.run_once()

    endpoint = endpoint_of(user_id)
    assert endpoint.next_attempt_at == endpoint.paused_until
    # The event itself is due: it goes as soon as the pause ends.
    assert seconds_until(deliveries()[0].next_attempt_at) <= 0


def test_a_sender_holding_the_endpoint_keeps_its_lease(client, auth_headers, worker):
    """The sender reschedules when it is done, and sees the new event then."""
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    token = uuid.uuid4()
    update_endpoint(user_id, claim_token=token, next_attempt_at=func.now() + timedelta(seconds=90))
    lease = endpoint_of(user_id).next_attempt_at

    worker.run_once()

    endpoint = endpoint_of(user_id)
    assert (endpoint.claim_token, endpoint.next_attempt_at) == (token, lease)
    assert len(deliveries()) == 1


def test_an_endpoint_with_a_leftover_claim_but_no_gate_is_woken(client, auth_headers, worker):
    """Never left that way by the code, but an event must not wait forever if it were."""
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    update_endpoint(user_id, claim_token=uuid.uuid4(), next_attempt_at=None)

    worker.run_once()

    assert -5 < seconds_until(endpoint_of(user_id).next_attempt_at) <= 0


def age(job_id, days=1, seconds=1) -> None:
    """As if the job, and so its event, had finished that long ago."""
    moment = func.now() - timedelta(days=days, seconds=seconds)
    update_job(job_id, finished_at=moment)
    with get_sessionmaker()() as db:
        db.execute(
            WebhookDelivery.__table__.update()
            .where(WebhookDelivery.job_id == uuid.UUID(str(job_id)))
            .values(created_at=moment)
        )
        db.commit()


def test_a_pending_event_is_never_deleted_with_its_job(client, auth_headers, worker):
    """Its job is kept until the event is given up on; then both go."""
    assert timedelta(days=1) >= RETRY_FOR
    assert type(get_settings()).model_fields["job_retention_days"].metadata[0].gt == 0
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    worker.run_once()
    age(job["id"])

    with get_sessionmaker()() as db:
        assert prune_finished(db, 1) == 0
    assert [d.state for d in deliveries()] == ["pending"]

    assert give_up_on_expired_events(get_sessionmaker()) == 1

    assert [(d.state, d.last_error) for d in deliveries()] == [("failed", GAVE_UP)]
    assert endpoint_of(user_id_of(auth_headers)).next_attempt_at is None
    with get_sessionmaker()() as db:
        assert prune_finished(db, 1) == 1
    assert deliveries() == []


def test_the_workers_prune_gives_up_on_events_no_sender_looks_at(client, auth_headers, worker):
    """Without any sender (WEBHOOK_SENDERS=0, say), events still end, and their jobs go."""
    set_webhook(client, auth_headers)
    old, recent = queued(client, auth_headers), queued(client, auth_headers)
    worker.run_once()
    worker.run_once()
    age(old["id"], days=8)

    worker.prune()

    assert db_job(old["id"]) is None
    assert [str(d.job_id) for d in deliveries()] == [recent["id"]]
    assert deliveries()[0].state == "pending"


def test_giving_up_leaves_an_endpoint_a_sender_holds_to_it(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    job = queued(client, auth_headers)
    worker.run_once()
    age(job["id"])

    with get_sessionmaker()() as holder:
        holder.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.user_id == user_id).with_for_update()
        )
        assert give_up_on_expired_events(get_sessionmaker()) == 0

    assert give_up_on_expired_events(get_sessionmaker()) == 1


def test_pruning_never_waits_for_a_pending_event(client, auth_headers, worker):
    """Senders lock pending events (after their endpoint); pruning only deletes jobs whose event
    has ended, so it never waits for them (nor they for it)."""
    set_webhook(client, auth_headers)
    pending, done = queued(client, auth_headers), queued(client, auth_headers)
    worker.run_once()
    worker.run_once()
    for job in (pending, done):
        age(job["id"])
    finished = delivery_of(done["id"])
    with get_sessionmaker()() as db:
        db.execute(
            WebhookDelivery.__table__.update()
            .where(WebhookDelivery.id == finished.id)
            .values(state="delivered")
        )
        db.commit()
    outcome = {}

    with get_sessionmaker()() as holder:
        holder.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.job_id == uuid.UUID(pending["id"]))
            .with_for_update()
        )

        def prune():
            with get_sessionmaker()() as db:
                outcome["deleted"] = prune_finished(db, 1)

        pruner = threading.Thread(target=prune)
        pruner.start()
        pruner.join(5)
        assert not pruner.is_alive()

    assert outcome == {"deleted": 1}
    assert db_job(done["id"]) is None
    assert db_job(pending["id"]) is not None


def test_deleting_a_user_deletes_their_webhook_and_events(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    queued(client, auth_headers)
    worker.run_once()
    bob = register(client, "bob")
    set_webhook(client, bob)

    with get_sessionmaker()() as db:
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        db.commit()

    assert deliveries() == []
    assert endpoint_of(user_id) is None
    with get_sessionmaker()() as db:
        assert db.scalar(select(func.count()).select_from(WebhookEndpoint)) == 1
