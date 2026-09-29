import os

# Configure the app for tests before anything imports its settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc_test"
)
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters-long"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.storage import LocalStorage, get_storage  # noqa: E402
from tests.utils import register  # noqa: E402


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
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


@pytest.fixture
def client(clean_db, storage):
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers(client) -> dict:
    return register(client)
