import base64
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import Job, WebhookDelivery, WebhookEndpoint
from app.webhooks import get_webhook_secrets
from tests.utils import (
    WEBHOOK_URL,
    deliveries,
    endpoint_of,
    make_image_bytes,
    register,
    seconds_until,
    set_webhook,
    update_endpoint,
    user_id_of,
    wait_for_a_lock_wait,
)


@pytest.fixture
def settings(client):
    """The settings the API uses, which a test may change."""
    settings = get_settings().model_copy()
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


def put(client, headers, url):
    return client.put("/me/webhook", headers=headers, json={"url": url})


def add_delivery(user_id, client, headers, **values) -> WebhookDelivery:
    """A delivery of a new (queued) job of the user's, as if that job had finished."""
    files = {"file": ("a.png", make_image_bytes(), "image/png")}
    image = client.post("/images", headers=headers, files=files).json()
    with get_sessionmaker()() as db:
        job = Job(
            owner_id=user_id, source_image_id=uuid.UUID(image["id"]), transformations={}, attempts=0
        )
        db.add(job)
        db.flush()
        delivery = WebhookDelivery(job_id=job.id, user_id=user_id, type="job.succeeded", **values)
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        return delivery


# --- Setting it -----------------------------------------------------------------------------------


def test_setting_a_webhook_returns_its_secret_once(client, auth_headers):
    user_id = user_id_of(auth_headers)

    response = put(client, auth_headers, WEBHOOK_URL)

    assert response.status_code == 201
    assert response.headers["location"] == "http://testserver/me/webhook"
    assert response.headers["cache-control"] == "no-store"
    created = response.json()
    assert created["url"] == WEBHOOK_URL
    assert created["secret"].startswith("whsec_")
    assert len(base64.b64decode(created["secret"].removeprefix("whsec_"))) == 32
    endpoint = endpoint_of(user_id)
    assert created["secret"] == get_webhook_secrets().secret(user_id, endpoint.secret_salt)
    assert created["pending_deliveries"] == 0
    assert created["consecutive_failures"] == 0
    assert created["paused_until"] is None
    assert created["previous_secret_expires_at"] is None
    assert endpoint.next_attempt_at is None and endpoint.claim_token is None

    shown = client.get("/me/webhook", headers=auth_headers)

    assert shown.status_code == 200
    assert shown.json() == {key: value for key, value in created.items() if key != "secret"}
    assert "cache-control" not in shown.headers


def test_secrets_differ_between_users_and_registrations(client, auth_headers):
    first = set_webhook(client, auth_headers)["secret"]
    bob = set_webhook(client, register(client, "bob"))["secret"]
    assert client.delete("/me/webhook", headers=auth_headers).status_code == 204

    again = set_webhook(client, auth_headers)["secret"]

    assert len({first, bob, again}) == 3


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("HTTPS://Hooks.Example.COM/In?A=1", "https://hooks.example.com/In?A=1"),
        ("https://hooks.example.com", "https://hooks.example.com/"),
        ("https://hooks.example.com:8443/a;b?c=d", "https://hooks.example.com:8443/a;b?c=d"),
        ("https://hooks.example.com:/x", "https://hooks.example.com/x"),
        ("https://[2606:4700:0::1111]/x", "https://[2606:4700::1111]/x"),
        ("https://8.8.8.8/x", "https://8.8.8.8/x"),
    ],
)
def test_urls_are_stored_normalized(client, auth_headers, given, stored):
    assert set_webhook(client, auth_headers, given)["url"] == stored


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://hooks.example.com/", "must start with https://"),
        ("https://localhost/", "must have a public host name"),
        ("https://127.0.0.1/", "non-public address"),
        ("https://169.254.169.254/latest/meta-data", "non-public address"),
        ("https://user:pass@hooks.example.com/", "user name or password"),
        ("https://hooks.example.com/#x", "fragment"),
        ("https://[::1/", "is not a valid URL"),
    ],
)
def test_unusable_urls_are_refused(client, auth_headers, url, reason):
    response = put(client, auth_headers, url)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("The webhook URL ")
    assert reason in response.json()["detail"]
    assert endpoint_of(user_id_of(auth_headers)) is None


def test_overlong_urls_are_refused(client, auth_headers):
    url = "https://hooks.example.com/" + "a" * 2023
    assert put(client, auth_headers, url).status_code == 422
    assert put(client, auth_headers, url[:-1]).status_code == 201


def test_unknown_fields_are_refused(client, auth_headers):
    response = client.put(
        "/me/webhook", headers=auth_headers, json={"url": WEBHOOK_URL, "events": ["job.failed"]}
    )
    assert response.status_code == 422


def test_private_networks_are_allowed_only_in_development(client, auth_headers, settings):
    assert put(client, auth_headers, "http://127.0.0.1:9000/in").status_code == 422

    settings.webhook_allow_private_networks = True

    assert put(client, auth_headers, "http://127.0.0.1:9000/in").status_code == 201
    assert put(client, auth_headers, "http://169.254.169.254/").status_code == 422


def test_setting_the_same_url_again_changes_nothing(client, auth_headers):
    user_id = user_id_of(auth_headers)
    created = set_webhook(client, auth_headers)
    update_endpoint(user_id, consecutive_failures=2, paused_until=func.now() + timedelta(hours=1))
    before = endpoint_of(user_id)

    response = put(client, auth_headers, WEBHOOK_URL)

    assert response.status_code == 200
    assert "secret" not in response.json()
    assert response.json()["updated_at"] == created["updated_at"]
    after = endpoint_of(user_id)
    for column in WebhookEndpoint.__table__.columns.keys():  # noqa: SIM118
        assert getattr(after, column) == getattr(before, column), column


def test_changing_the_url_keeps_the_secret_and_clears_the_pause(client, auth_headers):
    user_id = user_id_of(auth_headers)
    created = set_webhook(client, auth_headers)
    salt = endpoint_of(user_id).secret_salt
    update_endpoint(user_id, consecutive_failures=5, paused_until=func.now() + timedelta(hours=1))

    response = put(client, auth_headers, "https://other.example.com/hook")

    assert response.status_code == 200
    changed = response.json()
    assert "secret" not in changed
    assert changed["url"] == "https://other.example.com/hook"
    assert changed["updated_at"] > created["updated_at"]
    assert changed["secret_created_at"] == created["secret_created_at"]
    assert (changed["consecutive_failures"], changed["paused_until"]) == (0, None)
    assert endpoint_of(user_id).secret_salt == salt


def test_changing_the_url_brings_no_event_forward(client, auth_headers):
    """The pause ends, but each event keeps the time of its next attempt: changing the URL over
    and over can't make anything be sent sooner."""
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    delivery = add_delivery(
        user_id, client, auth_headers, next_attempt_at=func.now() + timedelta(minutes=10)
    )
    update_endpoint(
        user_id,
        paused_until=func.now() + timedelta(hours=1),
        next_attempt_at=func.now() + timedelta(hours=1),
    )

    assert put(client, auth_headers, "https://other.example.com/hook").status_code == 200

    gate = endpoint_of(user_id).next_attempt_at
    assert gate == deliveries(user_id)[0].next_attempt_at
    assert 590 < seconds_until(gate) <= 600
    assert delivery.state == "pending"


def test_changing_the_url_leaves_a_senders_claim_alone(client, auth_headers):
    """The sender's attempt to the old URL counts for nothing (it checks the URL when recording
    it), but it keeps the endpoint until then: one request at a time, whatever the user does."""
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    add_delivery(user_id, client, auth_headers)
    token = uuid.uuid4()
    update_endpoint(user_id, claim_token=token, next_attempt_at=func.now() + timedelta(seconds=90))
    before = endpoint_of(user_id)

    for n in range(5):
        assert put(client, auth_headers, f"https://other.example.com/{n}").status_code == 200

    after = endpoint_of(user_id)
    assert (after.claim_token, after.next_attempt_at) == (token, before.next_attempt_at)
    assert after.url == "https://other.example.com/4"


def test_a_first_put_racing_another_creates_one_webhook(client, auth_headers):
    """The second insert waits for the first one's row, then finds it: one webhook, whose secret
    only the first answer shows, at the second request's URL."""
    user_id = user_id_of(auth_headers)
    engine = get_sessionmaker().kw["bind"]
    first_thread = threading.get_ident()
    responses = {}

    def second():
        responses["second"] = put(client, auth_headers, "https://second.example.com/")

    other = threading.Thread(target=second)
    paused = []

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO webhook_endpoints" in statement and not paused:
            paused.append(threading.get_ident())
            other.start()
            assert wait_for_a_lock_wait()

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        responses["first"] = put(client, auth_headers, "https://first.example.com/")
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
        if other.ident:
            other.join(10)

    assert paused and paused[0] != first_thread  # the API ran it in its thread pool
    assert responses["first"].status_code == 201
    assert responses["second"].status_code == 200
    assert "secret" not in responses["second"].json()
    with get_sessionmaker()() as db:
        assert db.scalar(select(func.count()).select_from(WebhookEndpoint)) == 1
    endpoint = endpoint_of(user_id)
    assert endpoint.url == "https://second.example.com/"
    secret = get_webhook_secrets().secret(user_id, endpoint.secret_salt)
    assert responses["first"].json()["secret"] == secret


def test_a_put_that_loses_its_webhook_to_a_delete_creates_it_again(client, auth_headers):
    user_id = user_id_of(auth_headers)
    old_secret = set_webhook(client, auth_headers)["secret"]
    engine = get_sessionmaker().kw["bind"]
    deleted = []

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        # The insert found the webhook: delete it before it is locked.
        if "INSERT INTO webhook_endpoints" in statement and cursor.rowcount == 0 and not deleted:
            remover = threading.Thread(
                target=lambda: deleted.append(client.delete("/me/webhook", headers=auth_headers))
            )
            remover.start()
            remover.join(10)

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        response = put(client, auth_headers, "https://new.example.com/")
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)

    assert [r.status_code for r in deleted] == [204]
    assert response.status_code == 201
    assert response.json()["secret"] != old_secret
    assert endpoint_of(user_id).url == "https://new.example.com/"


def test_webhooks_are_off_without_a_public_base_url(client, auth_headers, settings):
    set_webhook(client, auth_headers)
    settings.public_base_url = ""

    response = put(client, auth_headers, "https://other.example.com/")

    assert response.status_code == 503
    assert response.json() == {"detail": "Webhooks are not enabled on this server"}
    # Users can still see and remove theirs.
    assert client.get("/me/webhook", headers=auth_headers).json()["url"] == WEBHOOK_URL
    assert client.post("/me/webhook/secret", headers=auth_headers).status_code == 200
    assert client.delete("/me/webhook", headers=auth_headers).status_code == 204


# --- Seeing it ------------------------------------------------------------------------------------


def test_no_webhook_is_a_404(client, auth_headers):
    response = client.get("/me/webhook", headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "No webhook is set"}


def test_users_only_see_their_own_webhook(client, auth_headers):
    set_webhook(client, auth_headers)
    bob = register(client, "bob")

    assert client.get("/me/webhook", headers=bob).status_code == 404
    assert client.delete("/me/webhook", headers=bob).status_code == 404
    assert client.post("/me/webhook/secret", headers=bob).status_code == 404
    assert client.get("/me/webhook", headers=auth_headers).status_code == 200


def test_the_webhook_shows_how_deliveries_are_going(client, auth_headers):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    add_delivery(user_id, client, auth_headers)
    add_delivery(user_id, client, auth_headers)
    add_delivery(user_id, client, auth_headers, state="delivered")
    update_endpoint(user_id, consecutive_failures=3, paused_until=func.now() + timedelta(minutes=2))

    shown = client.get("/me/webhook", headers=auth_headers).json()

    assert (shown["pending_deliveries"], shown["consecutive_failures"]) == (2, 3)
    assert shown["paused_until"] is not None

    update_endpoint(user_id, paused_until=func.now() - timedelta(seconds=1))

    assert client.get("/me/webhook", headers=auth_headers).json()["paused_until"] is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/me/webhook"),
        ("get", "/me/webhook"),
        ("delete", "/me/webhook"),
        ("post", "/me/webhook/secret"),
    ],
)
def test_webhooks_need_a_token(client, method, path):
    kwargs = {"json": {"url": WEBHOOK_URL}} if method == "put" else {}
    assert client.request(method, path, **kwargs).status_code == 401


# --- Rotating its secret --------------------------------------------------------------------------


def test_rotating_keeps_the_previous_secret_for_a_day_by_default(client, auth_headers):
    user_id = user_id_of(auth_headers)
    created = set_webhook(client, auth_headers)
    old_salt = endpoint_of(user_id).secret_salt

    response = client.post("/me/webhook/secret", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    rotated = response.json()
    endpoint = endpoint_of(user_id)
    assert rotated["secret"] == get_webhook_secrets().secret(user_id, endpoint.secret_salt)
    assert rotated["secret"] != created["secret"]
    assert endpoint.previous_secret_salt == old_salt
    assert 86390 < seconds_until(endpoint.previous_secret_expires_at) <= 86400
    assert rotated["previous_secret_expires_at"] is not None
    assert rotated["secret_created_at"] > created["secret_created_at"]
    assert rotated["url"] == created["url"]
    assert rotated["updated_at"] == created["updated_at"]


@pytest.mark.parametrize("grace", [1, 604800])
def test_the_previous_secret_can_be_kept_up_to_a_week(client, auth_headers, grace):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)

    response = client.post(
        "/me/webhook/secret",
        headers=auth_headers,
        json={"previous_secret_expires_in": grace},
    )

    assert response.status_code == 200
    assert grace - 10 < seconds_until(endpoint_of(user_id).previous_secret_expires_at) <= grace


def test_rotating_with_no_grace_drops_the_previous_secret_at_once(client, auth_headers):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    client.post("/me/webhook/secret", headers=auth_headers)

    response = client.post(
        "/me/webhook/secret", headers=auth_headers, json={"previous_secret_expires_in": 0}
    )

    assert response.json()["previous_secret_expires_at"] is None
    endpoint = endpoint_of(user_id)
    assert (endpoint.previous_secret_salt, endpoint.previous_secret_expires_at) == (None, None)


def test_only_the_latest_previous_secret_is_kept(client, auth_headers):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    client.post("/me/webhook/secret", headers=auth_headers)
    middle = endpoint_of(user_id).secret_salt

    client.post("/me/webhook/secret", headers=auth_headers)

    assert endpoint_of(user_id).previous_secret_salt == middle


@pytest.mark.parametrize(
    "body",
    [
        {"previous_secret_expires_in": -1},
        {"previous_secret_expires_in": 604801},
        {"previous_secret_expires_in": "soon"},
        {"grace": 10},
    ],
)
def test_invalid_rotations_are_refused(client, auth_headers, body):
    set_webhook(client, auth_headers)
    salt = endpoint_of(user_id_of(auth_headers)).secret_salt

    assert client.post("/me/webhook/secret", headers=auth_headers, json=body).status_code == 422
    assert endpoint_of(user_id_of(auth_headers)).secret_salt == salt


# --- Removing it ----------------------------------------------------------------------------------


def test_removing_the_webhook_cancels_what_is_pending(client, auth_headers):
    user_id = user_id_of(auth_headers)
    set_webhook(client, auth_headers)
    pending = add_delivery(user_id, client, auth_headers)
    delivered = add_delivery(user_id, client, auth_headers, state="delivered", attempts=1)
    bob = register(client, "bob")
    set_webhook(client, bob)
    others = add_delivery(user_id_of(bob), client, bob)

    response = client.delete("/me/webhook", headers=auth_headers)

    assert response.status_code == 204
    assert client.get("/me/webhook", headers=auth_headers).status_code == 404
    assert client.delete("/me/webhook", headers=auth_headers).status_code == 404
    states = {d.id: d for d in deliveries()}
    assert states[pending.id].state == "canceled"
    assert states[pending.id].last_error == "Webhook removed"
    assert states[pending.id].next_attempt_at is None
    assert states[pending.id].finished_at is not None
    assert states[delivered.id].state == "delivered"
    assert states[delivered.id].last_error is None
    assert states[others.id].state == "pending"


# --- The OpenAPI document -------------------------------------------------------------------------


def test_openapi_documents_the_webhook_and_its_events():
    document = app.openapi()
    paths = document["paths"]

    assert set(paths["/me/webhook"]) == {"put", "get", "delete"}
    assert "post" in paths["/me/webhook/secret"]
    created = paths["/me/webhook"]["put"]["responses"]["201"]
    assert created["content"]["application/json"]["schema"]["$ref"].endswith("/WebhookSecretOut")
    assert set(document["webhooks"]) == {"job.succeeded", "job.failed"}
    for operation in document["webhooks"].values():
        body = operation["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("/WebhookEvent")
        headers = {p["name"] for p in operation["post"]["parameters"] if p["in"] == "header"}
        assert headers == {"webhook-id", "webhook-timestamp", "webhook-signature"}
    schemas = document["components"]["schemas"]
    assert schemas["WebhookEvent"]["properties"]["data"]["$ref"].endswith("/JobData")
    assert "webhook" not in schemas["JobData"]["properties"]
    assert "webhook" in schemas["JobOut"]["properties"]
