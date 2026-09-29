import io
import uuid

import pytest
from PIL import Image

from app.config import get_settings
from app.main import app
from tests.utils import make_image_bytes, register


def upload(client, headers, data=None, filename="photo.png", content_type="image/png"):
    data = make_image_bytes() if data is None else data
    return client.post("/images", headers=headers, files={"file": (filename, data, content_type)})


def uploaded(client, headers, **kwargs) -> dict:
    response = upload(client, headers, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# --- Upload ---------------------------------------------------------------------------------------


def test_upload_returns_metadata(client, auth_headers, storage):
    data = make_image_bytes("PNG", size=(64, 48))

    response = upload(client, auth_headers, data=data, filename="cat.png")

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "cat.png"
    assert body["format"] == "png"
    assert body["mime_type"] == "image/png"
    assert (body["width"], body["height"]) == (64, 48)
    assert body["size_bytes"] == len(data)
    assert body["parent_id"] is None
    assert body["transformations"] is None
    assert body["url"] == f"http://testserver/images/{body['id']}/content"
    assert len(list(storage.root.rglob("*.png"))) == 1


def test_upload_detects_format_from_content_not_filename(client, auth_headers):
    body = uploaded(
        client,
        auth_headers,
        data=make_image_bytes("JPEG"),
        filename="x.png",
        content_type="image/png",
    )

    assert body["format"] == "jpeg"
    assert body["mime_type"] == "image/jpeg"


def test_upload_strips_directories_from_filename(client, auth_headers):
    body = uploaded(client, auth_headers, filename="C:\\Users\\me\\..\\secret.png")

    assert body["original_filename"] == "secret.png"


def test_upload_rejects_files_that_are_not_images(client, auth_headers):
    response = upload(client, auth_headers, data=b"hello", filename="notes.png")

    assert response.status_code == 400
    assert response.json()["detail"] == "File is not a valid image"


def test_upload_rejects_unsupported_formats(client, auth_headers):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PCX")

    response = upload(client, auth_headers, data=buffer.getvalue(), filename="old.pcx")

    assert response.status_code == 415


def test_upload_rejects_empty_file(client, auth_headers):
    assert upload(client, auth_headers, data=b"").status_code == 400


def test_upload_rejects_files_over_size_limit(client, auth_headers):
    small_limit = get_settings().model_copy(update={"max_upload_bytes": 100})
    app.dependency_overrides[get_settings] = lambda: small_limit

    response = upload(client, auth_headers, data=make_image_bytes(size=(200, 200)) + b"\0" * 100)

    assert response.status_code == 413


def test_upload_requires_authentication(client):
    assert upload(client, headers={}).status_code == 401


def test_upload_requires_a_file(client, auth_headers):
    assert client.post("/images", headers=auth_headers).status_code == 422


# --- Retrieve -------------------------------------------------------------------------------------


def test_get_image_metadata(client, auth_headers):
    image = uploaded(client, auth_headers)

    response = client.get(f"/images/{image['id']}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == image


def test_get_unknown_image_is_404(client, auth_headers):
    assert client.get(f"/images/{uuid.uuid4()}", headers=auth_headers).status_code == 404


def test_other_users_images_are_hidden(client, auth_headers):
    image = uploaded(client, auth_headers)
    bob = register(client, "bob")

    assert client.get(f"/images/{image['id']}", headers=bob).status_code == 404
    assert client.get(f"/images/{image['id']}/content", headers=bob).status_code == 404
    transform = client.post(
        f"/images/{image['id']}/transform", headers=bob, json={"transformations": {"flip": True}}
    )
    assert transform.status_code == 404
    assert client.delete(f"/images/{image['id']}", headers=bob).status_code == 404


def test_content_returns_original_bytes(client, auth_headers):
    data = make_image_bytes("PNG")
    image = uploaded(client, auth_headers, data=data)

    response = client.get(image["url"], headers=auth_headers)

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["etag"] == f'"{uuid.UUID(image["id"]).hex}"'
    assert "max-age" in response.headers["cache-control"]


def test_content_supports_conditional_requests(client, auth_headers):
    image = uploaded(client, auth_headers)
    etag = client.get(image["url"], headers=auth_headers).headers["etag"]

    response = client.get(image["url"], headers={**auth_headers, "If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""


@pytest.mark.parametrize(
    ("requested", "mime_type", "pillow_format"),
    [("webp", "image/webp", "WEBP"), ("jpg", "image/jpeg", "JPEG"), ("GIF", "image/gif", "GIF")],
)
def test_content_converts_format_on_the_fly(
    client, auth_headers, requested, mime_type, pillow_format
):
    image = uploaded(client, auth_headers)

    response = client.get(image["url"], params={"format": requested}, headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == mime_type
    assert decode(response.content).format == pillow_format


def test_content_variants_have_distinct_etags(client, auth_headers):
    image = uploaded(client, auth_headers)

    original = client.get(image["url"], headers=auth_headers).headers["etag"]
    webp = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)

    assert webp.headers["etag"] != original


def test_content_quality_changes_size(client, auth_headers):
    noisy = Image.effect_noise((200, 200), 64).convert("RGB")
    buffer = io.BytesIO()
    noisy.save(buffer, format="JPEG", quality=95)
    image = uploaded(client, auth_headers, data=buffer.getvalue(), filename="noise.jpg")

    low = client.get(image["url"], params={"quality": 10}, headers=auth_headers)

    assert low.status_code == 200
    assert low.headers["content-type"] == "image/jpeg"
    assert len(low.content) < image["size_bytes"]


def test_content_rejects_unknown_format(client, auth_headers):
    image = uploaded(client, auth_headers)

    response = client.get(image["url"], params={"format": "svg"}, headers=auth_headers)

    assert response.status_code == 422


# --- Transform ------------------------------------------------------------------------------------


def test_transform_creates_new_image(client, auth_headers):
    source = uploaded(
        client, auth_headers, data=make_image_bytes(size=(100, 50)), filename="cat.png"
    )
    spec = {"resize": {"width": 50}, "filters": {"grayscale": True}, "format": "webp"}

    response = client.post(
        f"/images/{source['id']}/transform", headers=auth_headers, json={"transformations": spec}
    )

    assert response.status_code == 201
    result = response.json()
    assert result["id"] != source["id"]
    assert result["parent_id"] == source["id"]
    assert result["format"] == "webp"
    assert result["original_filename"] == "cat.webp"
    assert (result["width"], result["height"]) == (50, 25)
    assert result["transformations"] == spec

    content = client.get(result["url"], headers=auth_headers)
    assert content.headers["content-type"] == "image/webp"
    assert decode(content.content).size == (50, 25)

    # The source image is untouched.
    assert client.get(f"/images/{source['id']}", headers=auth_headers).json() == source


def test_transform_can_be_chained(client, auth_headers):
    source = uploaded(client, auth_headers, data=make_image_bytes(size=(100, 50)))
    first = client.post(
        f"/images/{source['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"rotate": 90}},
    ).json()

    second = client.post(
        f"/images/{first['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"crop": {"width": 10, "height": 10}}},
    )

    assert second.status_code == 201
    assert second.json()["parent_id"] == first["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"transformations": {}},
        {"transformations": {"filters": {"grayscal": True}}},
        {"transformations": {"resize": {}}},
        {"transformations": {"rotate": 720}},
        {"transformations": {"format": "svg"}},
        {"transformations": {"quality": 0}},
        {"transformations": {"crop": {"x": 90, "y": 0, "width": 50, "height": 10}}},
        {"transformations": {"resize": {"width": 1_000_000}}},
        {},
    ],
)
def test_transform_rejects_invalid_requests(client, auth_headers, body):
    source = uploaded(client, auth_headers, data=make_image_bytes(size=(100, 50)))

    response = client.post(f"/images/{source['id']}/transform", headers=auth_headers, json=body)

    assert response.status_code == 422


def test_transform_unknown_image_is_404(client, auth_headers):
    response = client.post(
        f"/images/{uuid.uuid4()}/transform",
        headers=auth_headers,
        json={"transformations": {"flip": True}},
    )

    assert response.status_code == 404


# --- List -----------------------------------------------------------------------------------------


def test_list_is_paginated_and_newest_first(client, auth_headers):
    ids = [uploaded(client, auth_headers, filename=f"{i}.png")["id"] for i in range(3)]

    first = client.get("/images", params={"page": 1, "limit": 2}, headers=auth_headers).json()
    second = client.get("/images", params={"page": 2, "limit": 2}, headers=auth_headers).json()

    assert [item["id"] for item in first["items"]] == [ids[2], ids[1]]
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert (first["page"], first["limit"], first["total"], first["pages"]) == (1, 2, 3, 2)


def test_list_defaults_and_empty_state(client, auth_headers):
    body = client.get("/images", headers=auth_headers).json()

    assert body == {"items": [], "page": 1, "limit": 10, "total": 0, "pages": 0}


def test_list_only_shows_own_images(client, auth_headers):
    uploaded(client, auth_headers)
    bob = register(client, "bob")

    assert client.get("/images", headers=bob).json()["total"] == 0


@pytest.mark.parametrize("params", [{"page": 0}, {"limit": 0}, {"limit": 101}])
def test_list_validates_pagination(client, auth_headers, params):
    assert client.get("/images", params=params, headers=auth_headers).status_code == 422


# --- Delete ---------------------------------------------------------------------------------------


def test_delete_removes_record_and_file(client, auth_headers, storage):
    image = uploaded(client, auth_headers)

    response = client.delete(f"/images/{image['id']}", headers=auth_headers)

    assert response.status_code == 204
    assert client.get(f"/images/{image['id']}", headers=auth_headers).status_code == 404
    assert list(storage.root.rglob("*.png")) == []


def test_delete_keeps_derived_images(client, auth_headers):
    source = uploaded(client, auth_headers)
    derived = client.post(
        f"/images/{source['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"mirror": True}},
    ).json()

    client.delete(f"/images/{source['id']}", headers=auth_headers)

    remaining = client.get(f"/images/{derived['id']}", headers=auth_headers)
    assert remaining.status_code == 200
    assert remaining.json()["parent_id"] is None
    assert client.get(remaining.json()["url"], headers=auth_headers).status_code == 200
