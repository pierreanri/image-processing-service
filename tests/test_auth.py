import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import get_settings
from tests.utils import register


def test_register_returns_user_and_token(client):
    response = client.post("/register", json={"username": "alice", "password": "password123"})

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["username"] == "alice"
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] == 3600
    assert "password" not in str(body["user"])


def test_register_duplicate_username_conflicts(client):
    register(client, "alice")

    response = client.post("/register", json={"username": "alice", "password": "otherpass123"})

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("username", "password"),
    [("al", "password123"), ("alice", "short"), ("bad name!", "password123")],
)
def test_register_validates_input(client, username, password):
    response = client.post("/register", json={"username": username, "password": password})

    assert response.status_code == 422


def test_login_with_valid_credentials(client):
    register(client, "alice", "password123")

    response = client.post("/login", json={"username": "alice", "password": "password123"})

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_with_wrong_password(client):
    register(client, "alice", "password123")

    response = client.post("/login", json={"username": "alice", "password": "wrong-password"})

    assert response.status_code == 401


def test_login_with_unknown_user(client):
    response = client.post("/login", json={"username": "nobody", "password": "password123"})

    assert response.status_code == 401


def test_protected_route_requires_token(client):
    response = client.get("/images")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_protected_route_accepts_login_token(client):
    register(client, "alice", "password123")
    token = client.post("/login", json={"username": "alice", "password": "password123"}).json()[
        "access_token"
    ]

    response = client.get("/images", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def _token(sub: str, expires_in: timedelta, secret: str | None = None) -> str:
    settings = get_settings()
    payload = {"sub": sub, "exp": datetime.now(UTC) + expires_in}
    return jwt.encode(payload, secret or settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        _token(str(uuid.uuid4()), timedelta(minutes=5)),  # unknown user
        _token("not-a-uuid", timedelta(minutes=5)),
        _token(str(uuid.uuid4()), timedelta(minutes=5), secret="x" * 40),  # wrong signature
    ],
)
def test_protected_route_rejects_invalid_tokens(client, token):
    response = client.get("/images", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_protected_route_rejects_expired_token(client):
    response = client.post("/register", json={"username": "alice", "password": "password123"})
    expired = _token(response.json()["user"]["id"], timedelta(minutes=-1))

    response = client.get("/images", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401
