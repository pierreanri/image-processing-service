"""Sending webhook events (app/webhooks.py: claim_delivery, record_attempt, WebhookSender) to
local receivers."""

import json
import logging
import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.exc import OperationalError

import app.webhooks
from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app as api
from app.models import WebhookDelivery, WebhookEndpoint
from app.routers.views import PublicUrls
from app.webhook_client import Outcome, post
from app.webhooks import (
    GAVE_UP,
    LEASE_SECONDS,
    WebhookSender,
    claim_delivery,
    event_body,
    get_webhook_secrets,
    record_attempt,
)
from app.worker import Worker, run_loop, webhook_sender
from tests.receiver import Reply
from tests.utils import (
    RecordingStop,
    deliveries,
    delivery_of,
    endpoint_of,
    make_image_bytes,
    register,
    seconds_until,
    set_webhook,
    update_delivery,
    update_endpoint,
    user_id_of,
    verify_webhook,
)

URLS = PublicUrls(api, "http://testserver")


@pytest.fixture(autouse=True)
def settings(client):
    """The API's and the sender's settings: local receivers allowed, short attempts."""
    settings = get_settings().model_copy(
        update={"webhook_allow_private_networks": True, "webhook_timeout_seconds": 2.0}
    )
    api.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture
def sender(settings) -> WebhookSender:
    return WebhookSender(get_sessionmaker(), settings, get_webhook_secrets(), URLS)


def hook(client, headers, receiver, path="/hook") -> str:
    """Point the user's webhook at `receiver`; returns its secret."""
    return set_webhook(client, headers, receiver.url(path))["secret"]


def finish_job(client, headers, worker, transformations=None) -> dict:
    files = {"file": ("a.png", make_image_bytes(), "image/png")}
    image = client.post("/images", headers=headers, files=files).json()
    response = client.post(
        f"/images/{image['id']}/transform",
        headers={**headers, "Prefer": "respond-async"},
        json={"transformations": transformations or {"flip": True}},
    )
    assert response.status_code == 202, response.text
    assert worker.run_once()
    return response.json()


def make_due(user_id) -> None:
    """As if time had passed: every pending event of the user, and their endpoint, are due."""
    with get_sessionmaker()() as db:
        db.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.user_id == user_id, WebhookDelivery.state == "pending")
            .values(next_attempt_at=func.now())
        )
        db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.user_id == user_id)
            .values(next_attempt_at=func.now(), paused_until=None)
        )
        db.commit()


def drain(sender, limit=50) -> int:
    """Run the sender until nothing is due; returns how many attempts it made."""
    for attempts in range(limit):
        if not sender.run_once():
            return attempts
    raise AssertionError("the sender never ran out of work")


def in_thread(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# --- Delivering -----------------------------------------------------------------------------------


def test_a_delivery_is_signed_and_describes_the_job(client, auth_headers, worker, sender, receiver):
    user_id = user_id_of(auth_headers)
    secret = hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)

    assert sender.run_once()
    assert not sender.run_once()

    [request] = receiver.requests
    delivery = delivery_of(job["id"])
    assert request.path == "/hook"
    assert request.headers["webhook-id"] == str(delivery.id)
    assert request.headers["content-type"] == "application/json"
    assert verify_webhook(secret, request.headers, request.body)
    shown = client.get(job["url"], headers=auth_headers).json()
    event = json.loads(request.body)
    assert event == {
        "id": str(delivery.id),
        "type": "job.succeeded",
        "timestamp": shown["finished_at"],
        "data": {key: value for key, value in shown.items() if key != "webhook"},
    }
    assert event["data"]["url"] == f"http://testserver/jobs/{job['id']}"
    result_id = event["data"]["result"]["id"]
    assert event["data"]["result"]["url"] == f"http://testserver/images/{result_id}/content"
    assert (delivery.state, delivery.attempts) == ("delivered", 1)
    assert (delivery.last_status_code, delivery.last_error) == (200, None)
    assert delivery.next_attempt_at is None and delivery.finished_at is not None
    assert shown["webhook"]["state"] == "delivered"
    assert shown["webhook"]["next_attempt_at"] is None
    endpoint = endpoint_of(user_id)
    assert (endpoint.claim_token, endpoint.next_attempt_at) == (None, None)
    assert (endpoint.consecutive_failures, endpoint.paused_until) == (0, None)


def test_a_failed_job_is_delivered_with_its_error(client, auth_headers, worker, sender, receiver):
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker, {"crop": {"x": 90, "width": 50, "height": 5}})

    assert sender.run_once()

    event = json.loads(receiver.requests[0].body)
    assert event["type"] == "job.failed"
    assert event["data"]["id"] == job["id"]
    assert event["data"]["error"]["status_code"] == 422
    assert event["data"]["result"] is None


def test_the_payload_describes_the_job_when_sent(client, auth_headers, worker, sender, receiver):
    """A result deleted before the event is sent is null in it, as in GET /jobs/{id}."""
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    result_id = client.get(job["url"], headers=auth_headers).json()["result"]["id"]
    assert client.delete(f"/images/{result_id}", headers=auth_headers).status_code == 204

    assert sender.run_once()

    data = json.loads(receiver.requests[0].body)["data"]
    assert (data["status"], data["result"]) == ("succeeded", None)


def test_payload_urls_keep_a_base_path():
    urls = PublicUrls(api, "https://api.example.com/imgsvc")
    job_id = uuid.uuid4()

    assert str(urls.url_for("get_job", job_id=job_id)) == (
        f"https://api.example.com/imgsvc/jobs/{job_id}"
    )


def test_events_go_out_oldest_first(client, auth_headers, worker, sender, receiver):
    hook(client, auth_headers, receiver)
    jobs = [finish_job(client, auth_headers, worker)["id"] for _ in range(3)]

    assert drain(sender) == 3

    assert [json.loads(r.body)["data"]["id"] for r in receiver.requests] == jobs


# --- Failing --------------------------------------------------------------------------------------


def test_failures_back_off_per_event_and_pause_the_endpoint(
    client, auth_headers, worker, sender, receiver
):
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    receiver.respond(Reply(status=500), Reply(status=500))

    for attempt, delay in ((1, 30), (2, 60)):
        assert sender.run_once()
        delivery = delivery_of(job["id"])
        assert (delivery.state, delivery.attempts) == ("pending", attempt)
        assert (delivery.last_status_code, delivery.last_error) == (500, "HTTP 500")
        assert delay - 5 < seconds_until(delivery.next_attempt_at) <= delay
        endpoint = endpoint_of(user_id)
        assert endpoint.consecutive_failures == attempt
        assert delay - 5 < seconds_until(endpoint.paused_until) <= delay
        assert endpoint.next_attempt_at >= delivery.next_attempt_at
        assert endpoint.claim_token is None
        shown = client.get(job["url"], headers=auth_headers).json()["webhook"]
        assert (shown["state"], shown["last_error"]) == ("pending", "HTTP 500")
        assert not sender.run_once()  # Not due yet.
        make_due(user_id)

    assert sender.run_once()

    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.attempts, delivery.last_error) == ("delivered", 3, None)
    endpoint = endpoint_of(user_id)
    assert (endpoint.consecutive_failures, endpoint.paused_until) == (0, None)
    assert len(receiver.requests) == 3
    assert len({r.headers["webhook-id"] for r in receiver.requests}) == 1


def test_retry_delays_double_up_to_an_hour():
    assert [app.webhooks.retry_delay(n) for n in range(1, 10)] == [
        30,
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
        3600,
    ]
    assert app.webhooks.retry_delay(10_000) == 3600


def test_retry_after_lengthens_the_pause_up_to_an_hour(
    client, auth_headers, worker, sender, receiver
):
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    receiver.respond(
        Reply(status=503, headers={"Retry-After": "120"}),
        Reply(status=429, headers={"Retry-After": "99999"}),
        Reply(status=503, headers={"Retry-After": "1"}),
    )

    # The endpoint waits as asked; the event keeps its own schedule.
    assert sender.run_once()
    assert 115 < seconds_until(endpoint_of(user_id).paused_until) <= 120
    assert 25 < seconds_until(delivery_of(job["id"]).next_attempt_at) <= 30
    assert endpoint_of(user_id).next_attempt_at == endpoint_of(user_id).paused_until
    make_due(user_id)

    assert sender.run_once()
    assert 3595 < seconds_until(endpoint_of(user_id).paused_until) <= 3600
    make_due(user_id)

    # Never shorter than the endpoint's own backoff (third failure in a row: 120 s).
    assert sender.run_once()
    assert 115 < seconds_until(endpoint_of(user_id).paused_until) <= 120


def test_a_dead_endpoint_gets_one_attempt_per_pause_whatever_its_backlog(
    client, auth_headers, worker, sender, closed_port
):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers, f"http://127.0.0.1:{closed_port}/hook")
    for _ in range(5):
        finish_job(client, auth_headers, worker)

    assert drain(sender) == 1

    attempted = [d for d in deliveries(user_id) if d.attempts]
    assert len(attempted) == 1
    assert attempted[0].last_error == "Could not connect"
    assert attempted[0].last_status_code is None
    assert 25 < seconds_until(endpoint_of(user_id).paused_until) <= 30
    assert client.get("/me/webhook", headers=auth_headers).json()["pending_deliveries"] == 5


def test_a_poison_event_does_not_block_the_others(client, auth_headers, worker, sender, receiver):
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    jobs = [finish_job(client, auth_headers, worker)["id"] for _ in range(3)]
    poison = delivery_of(jobs[0])
    receiver.per_event[str(poison.id)] = Reply(status=400)

    assert sender.run_once()
    assert delivery_of(jobs[0]).last_error == "HTTP 400"
    # Once the pause is over, the others go; the poison event waits for its own retry.
    update_endpoint(user_id, paused_until=None, next_attempt_at=func.now())

    assert drain(sender) == 2

    states = [delivery_of(job).state for job in jobs]
    assert states == ["pending", "delivered", "delivered"]
    assert endpoint_of(user_id).consecutive_failures == 0
    assert 0 < seconds_until(endpoint_of(user_id).next_attempt_at) <= 30


def test_redirects_are_not_followed(client, auth_headers, worker, sender, receivers):
    first, second = receivers(), receivers()
    first.default = Reply(status=307, headers={"Location": second.url()})
    hook(client, auth_headers, first)
    job = finish_job(client, auth_headers, worker)

    assert sender.run_once()

    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.last_status_code) == ("pending", 307)
    assert delivery.last_error == "HTTP 307 (redirects are not followed)"
    assert second.requests == []


def test_events_are_given_up_on_a_day_after_their_job_finished(
    client, auth_headers, worker, sender, receiver
):
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    old = finish_job(client, auth_headers, worker)
    update_delivery(
        delivery_of(old["id"]).id,
        created_at=func.now() - timedelta(hours=24, seconds=1),
        last_error="HTTP 502",
    )

    assert not sender.run_once()

    given_up = delivery_of(old["id"])
    assert (given_up.state, given_up.last_error) == ("failed", "HTTP 502")
    assert given_up.next_attempt_at is None and given_up.finished_at is not None
    assert receiver.requests == []
    endpoint = endpoint_of(user_id)
    assert (endpoint.next_attempt_at, endpoint.claim_token) == (None, None)
    shown = client.get(old["url"], headers=auth_headers).json()["webhook"]
    assert (shown["state"], shown["next_attempt_at"]) == ("failed", None)


def test_an_event_never_attempted_is_given_up_with_its_own_message(
    client, auth_headers, worker, sender, receiver
):
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    update_delivery(delivery_of(job["id"]).id, created_at=func.now() - timedelta(days=1, hours=1))

    assert not sender.run_once()

    assert (delivery_of(job["id"]).state, delivery_of(job["id"]).last_error) == ("failed", GAVE_UP)


def test_an_attempt_whose_retry_would_come_too_late_gives_up(
    client, auth_headers, worker, sender, receiver
):
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    # Ten minutes left, and the next retry would be 32 minutes away.
    update_delivery(
        delivery_of(job["id"]).id,
        created_at=func.now() - timedelta(hours=23, minutes=50),
        attempts=6,
    )
    receiver.default = Reply(status=500)

    assert sender.run_once()

    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.attempts, delivery.last_error) == ("failed", 7, "HTTP 500")
    assert delivery.next_attempt_at is None and delivery.finished_at is not None


# --- Claims, and what happens meanwhile -----------------------------------------------------------


def test_a_claim_holds_the_endpoint_and_schedules_the_retry(client, auth_headers, worker):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers, "https://hooks.example.com/in")
    job = finish_job(client, auth_headers, worker)
    finish_job(client, auth_headers, worker)

    with get_sessionmaker()() as db:
        claim = claim_delivery(db)
    with get_sessionmaker()() as db:
        assert claim_delivery(db) is None  # One attempt at a time per endpoint.

    endpoint = endpoint_of(user_id)
    assert endpoint.claim_token == claim.token
    assert LEASE_SECONDS - 5 < seconds_until(endpoint.next_attempt_at) <= LEASE_SECONDS
    delivery = delivery_of(job["id"])
    assert (claim.delivery_id, claim.attempts, delivery.attempts) == (delivery.id, 1, 1)
    # Should this attempt never be recorded, the event goes again on schedule.
    assert 25 < seconds_until(delivery.next_attempt_at) <= 30
    assert delivery.last_attempt_at is not None


def test_a_crashed_sender_is_taken_over_and_the_event_resent_on_schedule(
    client, auth_headers, worker, sender, receiver, settings
):
    user_id = user_id_of(auth_headers)
    secret = hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    with get_sessionmaker()() as db:
        crashed = claim_delivery(db)
    # It sent the event, then died before recording that.
    body = event_body(crashed, URLS)
    headers = get_webhook_secrets().headers(crashed.delivery_id, body, user_id, crashed.salts)
    assert post(crashed.url, headers, body, timeout=2, allow_private=True).delivered

    # Its lease runs out: the endpoint is free, but the event isn't due again yet.
    update_endpoint(user_id, next_attempt_at=func.now() - timedelta(seconds=1))
    assert not sender.run_once()
    endpoint = endpoint_of(user_id)
    assert endpoint.claim_token is None
    assert 0 < seconds_until(endpoint.next_attempt_at) <= 30

    make_due(user_id)
    assert sender.run_once()

    assert [r.headers["webhook-id"] for r in receiver.requests] == [str(crashed.delivery_id)] * 2
    assert all(verify_webhook(secret, r.headers, r.body) for r in receiver.requests)
    assert (delivery_of(job["id"]).state, delivery_of(job["id"]).attempts) == ("delivered", 2)
    # The crashed sender's outcome, should it come back, counts for nothing.
    with get_sessionmaker()() as db:
        assert record_attempt(db, crashed, Outcome(status_code=500, error="HTTP 500")) is False
    assert delivery_of(job["id"]).state == "delivered"
    assert endpoint_of(user_id).consecutive_failures == 0


def test_a_stale_claim_is_cleared_even_when_nothing_is_due(
    client, auth_headers, worker, sender, receiver
):
    """Otherwise, events queued later would leave the gate to a sender that is gone."""
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    first = finish_job(client, auth_headers, worker)
    with get_sessionmaker()() as db:
        assert claim_delivery(db) is not None
    # The sender died on the event's last attempt; then its lease ran out.
    update_delivery(
        delivery_of(first["id"]).id, created_at=func.now() - timedelta(hours=24, seconds=1)
    )
    update_endpoint(user_id, next_attempt_at=func.now() - timedelta(seconds=1))

    assert not sender.run_once()

    endpoint = endpoint_of(user_id)
    assert (endpoint.claim_token, endpoint.next_attempt_at) == (None, None)
    assert delivery_of(first["id"]).state == "failed"

    second = finish_job(client, auth_headers, worker)

    assert sender.run_once()
    assert delivery_of(second["id"]).state == "delivered"


def test_changing_the_url_during_an_attempt_sends_the_event_again_to_the_new_one(
    client, auth_headers, worker, sender, receivers
):
    user_id = user_id_of(auth_headers)
    release = threading.Event()
    first, second = receivers(), receivers()
    first.default = Reply(hang=release)
    hook(client, auth_headers, first)
    job = finish_job(client, auth_headers, worker)
    attempt = in_thread(sender.run_once)
    assert first.first_request.wait(5)

    assert (
        client.put("/me/webhook", headers=auth_headers, json={"url": second.url()}).status_code
        == 200
    )
    release.set()
    attempt.join(5)

    # The answer from the old URL counts for nothing: the event goes to the new one, on the
    # schedule its claim set.
    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.attempts, delivery.last_status_code) == ("pending", 1, None)
    assert 0 < seconds_until(delivery.next_attempt_at) <= 30
    endpoint = endpoint_of(user_id)
    assert endpoint.claim_token is None
    assert endpoint.next_attempt_at == delivery.next_attempt_at
    make_due(user_id)

    assert sender.run_once()

    assert delivery_of(job["id"]).state == "delivered"
    assert first.requests[0].headers["webhook-id"] == second.requests[0].headers["webhook-id"]


def test_changing_the_url_over_and_over_never_starts_another_request(
    client, auth_headers, worker, sender, receivers
):
    user_id = user_id_of(auth_headers)
    release = threading.Event()
    first, second = receivers(), receivers()
    first.default = Reply(hang=release)
    hook(client, auth_headers, first)
    job = finish_job(client, auth_headers, worker)
    attempt = in_thread(sender.run_once)
    assert first.first_request.wait(5)

    for n in range(10):
        response = client.put(
            "/me/webhook", headers=auth_headers, json={"url": second.url(f"/{n}")}
        )
        assert response.status_code == 200
        assert not sender.run_once()  # Still held by the attempt under way.
        assert seconds_until(delivery_of(job["id"]).next_attempt_at) > 0
    release.set()
    attempt.join(5)

    assert not sender.run_once()
    assert (len(first.requests), second.requests) == (1, [])
    assert first.peak_in_flight == 1
    assert endpoint_of(user_id).url == second.url("/9")


def test_removing_the_webhook_during_an_attempt_discards_it(
    client, auth_headers, worker, sender, receiver
):
    release = threading.Event()
    receiver.default = Reply(hang=release)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    attempt = in_thread(sender.run_once)
    assert receiver.first_request.wait(5)

    assert client.delete("/me/webhook", headers=auth_headers).status_code == 204
    release.set()
    attempt.join(5)

    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.last_error) == ("canceled", "Webhook removed")
    assert delivery.last_status_code is None
    assert endpoint_of(user_id_of(auth_headers)) is None


def test_a_canceled_event_is_not_marked_delivered(client, auth_headers, worker, receiver):
    """An attempt whose endpoint was removed and set again (new claims) can't revive it."""
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    with get_sessionmaker()() as db:
        claim = claim_delivery(db)
    update_delivery(claim.delivery_id, state="canceled", next_attempt_at=None)

    with get_sessionmaker()() as db:
        assert record_attempt(db, claim, Outcome(status_code=200))

    assert delivery_of(job["id"]).state == "canceled"
    assert endpoint_of(user_id).next_attempt_at is None


def test_one_request_at_a_time_per_endpoint(client, auth_headers, worker, sender, receiver):
    receiver.default = Reply(delay=0.2)
    hook(client, auth_headers, receiver)
    for _ in range(4):
        finish_job(client, auth_headers, worker)
    deadline = time.monotonic() + 15

    def send_until_done():
        while time.monotonic() < deadline:
            if all(d.state == "delivered" for d in deliveries()):
                return
            if not sender.run_once():
                time.sleep(0.02)

    threads = [in_thread(send_until_done) for _ in range(3)]
    for thread in threads:
        thread.join(20)

    assert [d.state for d in deliveries()] == ["delivered"] * 4
    assert receiver.peak_in_flight == 1
    assert len(receiver.requests) == 4


def test_endpoints_take_turns(client, auth_headers, worker, sender, receiver):
    hook(client, auth_headers, receiver, "/alice")
    for _ in range(3):
        finish_job(client, auth_headers, worker)
    bob = register(client, "bob")
    hook(client, bob, receiver, "/bob")
    for _ in range(3):
        finish_job(client, bob, worker)

    assert drain(sender) == 6

    assert [r.path for r in receiver.requests] == ["/alice", "/bob"] * 3


def test_a_hanging_endpoint_does_not_hold_up_another(
    client, auth_headers, worker, sender, receivers
):
    release = threading.Event()
    stuck, fine = receivers(), receivers()
    stuck.default = Reply(hang=release)
    hook(client, auth_headers, stuck)
    finish_job(client, auth_headers, worker)
    bob = register(client, "bob")
    hook(client, bob, fine)
    bobs = finish_job(client, bob, worker)
    attempt = in_thread(sender.run_once)
    assert stuck.first_request.wait(5)

    assert sender.run_once()

    assert delivery_of(bobs["id"]).state == "delivered"
    release.set()
    attempt.join(5)


# --- Other failures -------------------------------------------------------------------------------


def test_errors_building_a_request_fail_only_that_attempt(
    client, auth_headers, worker, sender, receiver, monkeypatch, caplog
):
    user_id = user_id_of(auth_headers)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)

    def broken(claim, urls):
        raise RuntimeError("bug")

    monkeypatch.setattr(app.webhooks, "event_body", broken)

    assert sender.run_once()

    delivery = delivery_of(job["id"])
    assert (delivery.state, delivery.last_error) == ("pending", "Internal error")
    assert 25 < seconds_until(delivery.next_attempt_at) <= 30
    endpoint = endpoint_of(user_id)
    assert (endpoint.consecutive_failures, endpoint.paused_until) == (0, None)
    assert receiver.requests == []
    assert "could not be sent" in caplog.text and "RuntimeError: bug" in caplog.text

    monkeypatch.undo()
    make_due(user_id)
    assert sender.run_once()
    assert delivery_of(job["id"]).state == "delivered"


def test_the_sender_loop_backs_off_on_errors_and_survives_bugs():
    outcomes = iter([OperationalError, RuntimeError, False, True, OperationalError])

    def step():
        outcome = next(outcomes, False)
        if isinstance(outcome, type):
            raise (
                outcome("SELECT 1", {}, Exception("gone"))
                if outcome is OperationalError
                else outcome("bug")
            )
        return outcome

    stop = RecordingStop(waits=5)

    run_loop(stop, step, 0.5, (Exception,))

    assert stop.waits == [1.0, 2.0, 0.5, 1.0, 0.5]


def test_logs_name_the_host_only(client, auth_headers, worker, sender, receiver, caplog):
    caplog.set_level(logging.INFO, logger="app.webhooks")
    secret = hook(client, auth_headers, receiver, "/private/path?token=receiver-credential")
    finish_job(client, auth_headers, worker)
    finish_job(client, auth_headers, worker)
    receiver.respond(Reply(status=500))

    assert sender.run_once()
    make_due(user_id_of(auth_headers))
    assert drain(sender) == 2

    assert "not delivered to 127.0.0.1 (attempt 1): HTTP 500" in caplog.text
    assert "delivered to 127.0.0.1 (attempt 2): HTTP 200" in caplog.text
    for hidden in ("receiver-credential", "/private", secret, secret.removeprefix("whsec_")):
        assert hidden not in caplog.text


def test_claims_keep_urls_and_secrets_out_of_their_repr(client, auth_headers, worker):
    set_webhook(client, auth_headers, "https://hooks.example.com/in?token=receiver-credential")
    finish_job(client, auth_headers, worker)

    with get_sessionmaker()() as db:
        claim = claim_delivery(db)

    text = repr(claim)
    assert str(claim.delivery_id) in text
    assert "receiver-credential" not in text and "hooks.example.com" not in text
    for salt in claim.salts:
        assert repr(salt) not in text and salt.hex() not in text


# --- Rotating secrets -----------------------------------------------------------------------------


def test_a_rotated_secret_signs_beside_the_new_one_until_its_grace_ends(
    client, auth_headers, worker, sender, receiver
):
    user_id = user_id_of(auth_headers)
    old = hook(client, auth_headers, receiver)
    new = client.post("/me/webhook/secret", headers=auth_headers).json()["secret"]
    finish_job(client, auth_headers, worker)

    assert sender.run_once()

    during = receiver.requests[-1]
    assert len(during.headers["webhook-signature"].split(" ")) == 2
    assert verify_webhook(new, during.headers, during.body)
    assert verify_webhook(old, during.headers, during.body)
    # The new secret's signature comes first.
    assert not verify_webhook(
        old,
        {**during.headers, "webhook-signature": during.headers["webhook-signature"].split()[0]},
        during.body,
    )

    update_endpoint(user_id, previous_secret_expires_at=func.now() - timedelta(seconds=1))
    finish_job(client, auth_headers, worker)
    assert sender.run_once()

    after = receiver.requests[-1]
    assert len(after.headers["webhook-signature"].split(" ")) == 1
    assert verify_webhook(new, after.headers, after.body)
    assert not verify_webhook(old, after.headers, after.body)

    newest = client.post(
        "/me/webhook/secret", headers=auth_headers, json={"previous_secret_expires_in": 0}
    ).json()["secret"]
    finish_job(client, auth_headers, worker)
    assert sender.run_once()

    last = receiver.requests[-1]
    assert verify_webhook(newest, last.headers, last.body)
    assert not verify_webhook(new, last.headers, last.body)


# --- The worker -----------------------------------------------------------------------------------


def worker_with_sender(storage, settings, sender, **changes) -> Worker:
    settings = settings.model_copy(update={"job_poll_seconds": 0.05, **changes})
    return Worker(get_sessionmaker(), storage, settings, sender)


def wait_until(condition, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_worker_runs_jobs_while_a_webhook_hangs(
    client, auth_headers, storage, settings, sender, receiver
):
    release = threading.Event()
    receiver.default = Reply(hang=release)
    hook(client, auth_headers, receiver)
    loop = worker_with_sender(storage, settings, sender, webhook_senders=1)
    stop = threading.Event()
    thread = in_thread(lambda: loop.run(stop))
    files = {"file": ("a.png", make_image_bytes(), "image/png")}

    def submit() -> str:
        image = client.post("/images", headers=auth_headers, files=files).json()
        return client.post(
            f"/images/{image['id']}/transform",
            headers={**auth_headers, "Prefer": "respond-async"},
            json={"transformations": {"flip": True}},
        ).json()["url"]

    try:
        submit()
        assert receiver.first_request.wait(10)
        second = submit()

        assert wait_until(
            lambda: client.get(second, headers=auth_headers).json()["status"] == "succeeded", 2
        )
    finally:
        stop.set()
        release.set()
        thread.join(10)
    assert not thread.is_alive()


def test_the_worker_stops_once_attempts_under_way_are_recorded(
    client, auth_headers, worker, storage, settings, sender, receiver
):
    user_id = user_id_of(auth_headers)
    receiver.default = Reply(delay=1)
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    loop = worker_with_sender(storage, settings, sender)
    stop = threading.Event()
    thread = in_thread(lambda: loop.run(stop))
    assert receiver.first_request.wait(10)

    stop.set()
    thread.join(settings.webhook_timeout_seconds + 1)

    assert not thread.is_alive()
    assert delivery_of(job["id"]).state == "delivered"
    assert endpoint_of(user_id).claim_token is None


def test_the_worker_starts_its_senders(
    client, auth_headers, worker, storage, settings, sender, receiver
):
    hook(client, auth_headers, receiver)
    job = finish_job(client, auth_headers, worker)
    loop = worker_with_sender(storage, settings, sender, webhook_senders=3)
    stop = threading.Event()
    thread = in_thread(lambda: loop.run(stop))
    try:
        assert wait_until(lambda: delivery_of(job["id"]).state == "delivered")
        names = {t.name for t in threading.enumerate()}
        assert {"webhook-sender-1", "webhook-sender-2", "webhook-sender-3"} <= names
    finally:
        stop.set()
        thread.join(10)
    assert not any(t.name.startswith("webhook-sender-") for t in threading.enumerate())


def test_workers_send_webhooks_only_when_configured(settings, caplog):
    caplog.set_level(logging.INFO, logger="app.worker")
    sessions = get_sessionmaker()

    assert webhook_sender(sessions, settings.model_copy(update={"public_base_url": ""})) is None
    assert webhook_sender(sessions, settings.model_copy(update={"webhook_senders": 0})) is None
    assert "PUBLIC_BASE_URL is not set" in caplog.text
    assert "WEBHOOK_SENDERS is 0" in caplog.text

    built = webhook_sender(sessions, settings)

    assert isinstance(built, WebhookSender)
    assert "WEBHOOK_ALLOW_PRIVATE_NETWORKS is on" in caplog.text
    caplog.clear()
    webhook_sender(sessions, settings.model_copy(update={"webhook_allow_private_networks": False}))
    assert "WEBHOOK_ALLOW_PRIVATE_NETWORKS" not in caplog.text
