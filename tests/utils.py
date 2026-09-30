import io
import uuid

import jwt
from fastapi.testclient import TestClient
from PIL import Image

from app.ratelimit import UNCHECKED, Limit, LimitState, RateLimitDecision
from app.storage import LocalStorage


def register(client: TestClient, username: str = "alice", password: str = "password123") -> dict:
    response = client.post("/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def user_id_of(headers: dict) -> uuid.UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])


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
