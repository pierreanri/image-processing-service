import io
import uuid

from fastapi.testclient import TestClient
from PIL import Image


def register(client: TestClient, username: str = "alice", password: str = "password123") -> dict:
    response = client.post("/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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
