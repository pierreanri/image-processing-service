"""The URL signing key API (/me/url-signing-key)."""

import re

import pytest
from sqlalchemy import func, select, text

from app.db import get_sessionmaker
from app.main import app
from app.models import UrlSigningKey
from app.signed_urls import get_url_signer
from tests.utils import create_url_signing_key, register, user_id_of


def stored_key(user_id) -> UrlSigningKey | None:
    with get_sessionmaker()() as db:
        return db.get(UrlSigningKey, user_id)


def test_creating_a_key_shows_it_once(client, auth_headers):
    response = client.post("/me/url-signing-key", headers=auth_headers)

    assert response.status_code == 201
    assert response.headers["location"] == "http://testserver/me/url-signing-key"
    assert response.headers["cache-control"] == "no-store"
    created = response.json()
    assert re.fullmatch(r"[A-Za-z0-9_-]{16}", created["key_id"])
    assert re.fullmatch(r"urlsec_[A-Za-z0-9_-]{43}", created["key"])
    assert created["key"] == get_url_signer().key(created["key_id"])
    assert created["url_prefix"] == f"http://testserver/signed/{created['key_id']}/"

    shown = client.get("/me/url-signing-key", headers=auth_headers)

    assert shown.status_code == 200
    assert shown.json() == {k: v for k, v in created.items() if k != "key"}
    assert "cache-control" not in shown.headers


def test_creating_a_second_key_is_refused(client, auth_headers):
    created = create_url_signing_key(client, auth_headers)

    response = client.post("/me/url-signing-key", headers=auth_headers)

    assert response.status_code == 409
    assert "rotate" in response.json()["detail"]
    assert stored_key(user_id_of(auth_headers)).key_id == created["key_id"]


def test_rotating_issues_a_new_key(client, auth_headers):
    created = create_url_signing_key(client, auth_headers)

    response = client.post("/me/url-signing-key/rotate", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    rotated = response.json()
    assert rotated["key_id"] != created["key_id"]
    assert rotated["key"] == get_url_signer().key(rotated["key_id"])
    assert rotated["url_prefix"].endswith(f"/signed/{rotated['key_id']}/")
    assert rotated["created_at"] > created["created_at"]
    assert stored_key(user_id_of(auth_headers)).key_id == rotated["key_id"]


def test_deleting_the_key(client, auth_headers):
    create_url_signing_key(client, auth_headers)

    assert client.delete("/me/url-signing-key", headers=auth_headers).status_code == 204

    assert stored_key(user_id_of(auth_headers)) is None
    assert client.get("/me/url-signing-key", headers=auth_headers).status_code == 404
    assert client.delete("/me/url-signing-key", headers=auth_headers).status_code == 404
    again = create_url_signing_key(client, auth_headers)
    assert again["key_id"] == stored_key(user_id_of(auth_headers)).key_id


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/me/url-signing-key"),
        ("post", "/me/url-signing-key/rotate"),
        ("delete", "/me/url-signing-key"),
    ],
)
def test_no_key_to_show_rotate_or_delete(client, auth_headers, method, path):
    response = client.request(method, path, headers=auth_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "No URL signing key"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/me/url-signing-key"),
        ("get", "/me/url-signing-key"),
        ("post", "/me/url-signing-key/rotate"),
        ("delete", "/me/url-signing-key"),
    ],
)
def test_keys_need_a_token(client, method, path):
    assert client.request(method, path).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.request(method, path, headers=bad).status_code == 401


def test_accounts_have_keys_of_their_own(client, auth_headers):
    alice = create_url_signing_key(client, auth_headers)
    bob_headers = register(client, "bob")
    bob = create_url_signing_key(client, bob_headers)

    assert alice["key_id"] != bob["key_id"] and alice["key"] != bob["key"]
    client.post("/me/url-signing-key/rotate", headers=bob_headers)
    assert stored_key(user_id_of(auth_headers)).key_id == alice["key_id"]


def test_only_the_key_id_is_stored(client, auth_headers):
    created = create_url_signing_key(client, auth_headers)

    with get_sessionmaker()() as db:
        row = db.execute(text("SELECT * FROM url_signing_keys")).one()
    assert set(row._mapping) == {"user_id", "key_id", "created_at"}
    assert created["key"] not in str(tuple(row))
    assert created["key"].removeprefix("urlsec_") not in str(tuple(row))


def test_deleting_the_user_deletes_the_key(client, auth_headers):
    create_url_signing_key(client, auth_headers)

    with get_sessionmaker()() as db:
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id_of(auth_headers)})
        db.commit()
        assert db.scalar(select(func.count()).select_from(UrlSigningKey)) == 0


def test_openapi_documents_the_key_api():
    paths = app.openapi()["paths"]

    assert set(paths["/me/url-signing-key"]) == {"post", "get", "delete"}
    assert "post" in paths["/me/url-signing-key/rotate"]
    created = paths["/me/url-signing-key"]["post"]["responses"]["201"]
    assert created["content"]["application/json"]["schema"]["$ref"].endswith(
        "/UrlSigningKeySecretOut"
    )
    for operations in (paths["/me/url-signing-key"], paths["/me/url-signing-key/rotate"]):
        for operation in operations.values():
            assert operation.get("security")
