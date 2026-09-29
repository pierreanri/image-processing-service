import pytest

from tests.conftest import register


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
