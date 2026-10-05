import io
import time
import uuid

import jwt
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, text, update

from app.db import get_sessionmaker
from app.models import WebhookDelivery, WebhookEndpoint
from app.ratelimit import UNCHECKED, Limit, LimitState, RateLimitDecision
from app.storage import LocalStorage


def register(client: TestClient, username: str = "alice", password: str = "password123") -> dict:
    response = client.post("/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def user_id_of(headers: dict) -> uuid.UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])


def share(client: TestClient, headers: dict, image_id: str, **body) -> dict:
    """Create a share link to an image (POST /images/{id}/share-links) and return it."""
    response = client.post(f"/images/{image_id}/share-links", headers=headers, json=body or None)
    assert response.status_code == 200, response.text
    return response.json()


def stored_keys(storage) -> list[str]:
    """Every key in a LocalStorage or S3Storage (temporary files included), sorted."""
    if isinstance(storage, LocalStorage):
        files = (path for path in storage.root.rglob("*") if path.is_file())
        return sorted(path.relative_to(storage.root).as_posix() for path in files)
    response = storage.client.list_objects_v2(Bucket=storage.bucket)
    return sorted(item["Key"] for item in response.get("Contents", []))


def make_image_bytes(
    fmt: str = "PNG", size: tuple[int, int] = (64, 48), mode: str = "RGB", color="red"
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class InMemoryVariantCache:
    """Stand-in for app.cache.VariantCache that keeps entries in a dict."""

    def __init__(self) -> None:
        self.entries: dict[uuid.UUID, dict[str, bytes]] = {}
        self.reads: list[tuple[uuid.UUID, str]] = []

    def get(self, image_id: uuid.UUID, variant: str) -> bytes | None:
        self.reads.append((image_id, variant))
        return self.entries.get(image_id, {}).get(variant)

    def set(self, image_id: uuid.UUID, variant: str, data: bytes) -> None:
        self.entries.setdefault(image_id, {})[variant] = data

    def invalidate(self, image_id: uuid.UUID) -> None:
        self.entries.pop(image_id, None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


MINUTE = Limit("minute", 30, 60)
HOUR = Limit("hour", 500, 3600)
ALLOWED = RateLimitDecision(True, (LimitState(MINUTE, 29, 60), LimitState(HOUR, 499, 3600)))
REJECTED = RateLimitDecision(False, (LimitState(MINUTE, 0, 13), LimitState(HOUR, 470, 2811)))


class FakeRateLimiter:
    """Stand-in for app.ratelimit.RateLimiter that records who was counted and returns a
    canned decision."""

    def __init__(self, decision: RateLimitDecision = UNCHECKED) -> None:
        self.decision = decision
        self.hits: list[uuid.UUID] = []

    def hit(self, user_id: uuid.UUID) -> RateLimitDecision:
        self.hits.append(user_id)
        return self.decision


def wait_for_a_lock_wait(timeout: float = 5) -> bool:
    """Until some session of the test database waits for a row lock."""
    engine = get_sessionmaker().kw["bind"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as probe:
            waiting = probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return True
        time.sleep(0.01)
    return False


# --- Webhooks -------------------------------------------------------------------------------------

WEBHOOK_URL = "https://hooks.example.com/in?source=imgsvc"


def set_webhook(client: TestClient, headers: dict, url: str = WEBHOOK_URL) -> dict:
    """PUT /me/webhook, expecting it to create the webhook; returns the body (with the secret)."""
    response = client.put("/me/webhook", headers=headers, json={"url": url})
    assert response.status_code == 201, response.text
    return response.json()


def endpoint_of(user_id: uuid.UUID) -> WebhookEndpoint | None:
    with get_sessionmaker()() as db:
        return db.get(WebhookEndpoint, user_id)


def update_endpoint(user_id: uuid.UUID, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(
            update(WebhookEndpoint).where(WebhookEndpoint.user_id == user_id).values(**values)
        )
        db.commit()


def deliveries(user_id: uuid.UUID | None = None) -> list[WebhookDelivery]:
    """Webhook deliveries (a user's), oldest first."""
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at, WebhookDelivery.id)
    if user_id is not None:
        query = query.where(WebhookDelivery.user_id == user_id)
    with get_sessionmaker()() as db:
        return list(db.scalars(query))


def delivery_of(job_id) -> WebhookDelivery | None:
    with get_sessionmaker()() as db:
        return db.scalar(
            select(WebhookDelivery).where(WebhookDelivery.job_id == uuid.UUID(str(job_id)))
        )


def update_delivery(delivery_id: uuid.UUID, **values) -> None:
    with get_sessionmaker()() as db:
        db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**values)
        )
        db.commit()


def seconds_until(moment) -> float:
    """Seconds from the database's clock to `moment`."""
    with get_sessionmaker()() as db:
        return (moment - db.scalar(select(text("now()")))).total_seconds()
