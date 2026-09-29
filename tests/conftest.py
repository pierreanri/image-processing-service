import io
import os

# Configure the app for tests before anything imports its settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc_test"
)
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters-long"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, get_engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def clean_db(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE images, users CASCADE"))


@pytest.fixture
def client(clean_db):
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, username: str = "alice", password: str = "password123") -> dict:
    response = client.post("/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def auth_headers(client) -> dict:
    return register(client)


def make_image_bytes(
    fmt: str = "PNG", size: tuple[int, int] = (64, 48), mode: str = "RGB", color="red"
) -> bytes:
    buffer = io.BytesIO()
    PILImage.new(mode, size, color).save(buffer, format=fmt)
    return buffer.getvalue()
