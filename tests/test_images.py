import io
import logging
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime

import pytest
import sqlalchemy.orm
from PIL import Image

from app.cache import VariantCache, get_variant_cache
from app.config import get_settings
from app.main import app
from app.ratelimit import Limit, RateLimiter, get_transform_rate_limiter
from app.redis_client import create_redis_client
from app.storage import CHUNK_SIZE
from tests.utils import ALLOWED, REJECTED, make_image_bytes, register, stored_keys, user_id_of


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


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
    keys = stored_keys(storage)
    assert len(keys) == 1
    assert keys[0].endswith(".png")


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
    assert response.headers["content-length"] == str(len(data))
    assert "transfer-encoding" not in response.headers
    assert response.headers["etag"] == f'"{uuid.UUID(image["id"]).hex}"'
    assert "max-age" in response.headers["cache-control"]
    last_modified = parsedate_to_datetime(response.headers["last-modified"])
    created_at = datetime.fromisoformat(image["created_at"])
    assert abs((last_modified - created_at).total_seconds()) < 1


def test_content_streams_large_originals_intact(client, auth_headers):
    # Several storage chunks' worth of incompressible pixels.
    noise = Image.effect_noise((400, 400), 100).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    data = buffer.getvalue()
    assert len(data) > 3 * CHUNK_SIZE
    image = uploaded(client, auth_headers, data=data)

    response = client.get(image["url"], headers=auth_headers)

    assert response.content == data
    assert response.headers["content-length"] == str(len(data))


def test_content_ignores_range_requests(client, auth_headers):
    data = make_image_bytes("PNG")
    image = uploaded(client, auth_headers, data=data)

    response = client.get(image["url"], headers={**auth_headers, "Range": "bytes=0-9"})

    # Ranges aren't supported: the whole image is sent, as HTTP allows.
    assert response.status_code == 200
    assert response.content == data
    assert "content-range" not in response.headers


def test_failed_commit_removes_the_stored_file(client, auth_headers, storage, monkeypatch):
    commit = sqlalchemy.orm.Session.commit

    def failing_commit(self):
        # Only the commit that inserts the image fails (not the read-only ones before it).
        if self.new:
            raise RuntimeError("database went away")
        commit(self)

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="database went away"):
        upload(client, auth_headers)

    assert stored_keys(storage) == []


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


# --- Conversion cache -----------------------------------------------------------------------------


def test_conversions_are_cached_per_format_and_quality(client, auth_headers, variant_cache):
    image = uploaded(client, auth_headers)
    image_id = uuid.UUID(image["id"])

    responses = {
        variant: client.get(image["url"], params=params, headers=auth_headers)
        for variant, params in [
            ("webp-qdefault", {"format": "webp"}),
            ("jpeg-q40", {"format": "jpeg", "quality": 40}),
            ("jpeg-qdefault", {"format": "jpg"}),
        ]
    }

    assert set(variant_cache.entries[image_id]) == set(responses)
    for variant, response in responses.items():
        assert response.status_code == 200
        assert variant_cache.entries[image_id][variant] == response.content
        assert response.headers["etag"] == f'"{image_id.hex}-{variant}"'


def test_lossless_conversions_share_one_entry_regardless_of_quality(
    client, auth_headers, variant_cache
):
    image = uploaded(client, auth_headers, data=make_image_bytes("JPEG"), filename="a.jpg")
    image_id = uuid.UUID(image["id"])

    plain = client.get(image["url"], params={"format": "png"}, headers=auth_headers)
    with_quality = client.get(
        image["url"], params={"format": "png", "quality": 10}, headers=auth_headers
    )

    assert set(variant_cache.entries[image_id]) == {"png-qdefault"}
    assert plain.headers["etag"] == with_quality.headers["etag"]
    assert plain.content == with_quality.content


def test_cached_conversion_is_served_without_reencoding(client, auth_headers, variant_cache):
    image = uploaded(client, auth_headers)
    image_id = uuid.UUID(image["id"])
    variant_cache.set(image_id, "webp-qdefault", b"cached-bytes")

    response = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.content == b"cached-bytes"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["etag"] == f'"{image_id.hex}-webp-qdefault"'
    assert "max-age" in response.headers["cache-control"]


def test_originals_and_not_modified_responses_skip_the_cache(client, auth_headers, variant_cache):
    image = uploaded(client, auth_headers)
    etag = f'"{uuid.UUID(image["id"]).hex}-webp-qdefault"'

    assert client.get(image["url"], headers=auth_headers).status_code == 200
    assert (
        client.get(image["url"], params={"format": "png"}, headers=auth_headers).status_code == 200
    )
    not_modified = client.get(
        image["url"], params={"format": "webp"}, headers={**auth_headers, "If-None-Match": etag}
    )

    assert not_modified.status_code == 304
    assert variant_cache.reads == []
    assert variant_cache.entries == {}


def test_quality_is_ignored_for_lossless_originals(client, auth_headers, variant_cache):
    data = make_image_bytes("PNG")
    image = uploaded(client, auth_headers, data=data)

    response = client.get(image["url"], params={"quality": 10}, headers=auth_headers)

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["etag"] == f'"{uuid.UUID(image["id"]).hex}"'
    assert variant_cache.reads == []


def test_cached_conversions_are_hidden_from_other_users(client, auth_headers, variant_cache):
    image = uploaded(client, auth_headers)
    variant_cache.set(uuid.UUID(image["id"]), "webp-qdefault", b"alice's image")
    bob = register(client, "bob")

    response = client.get(image["url"], params={"format": "webp"}, headers=bob)

    assert response.status_code == 404


def test_delete_invalidates_cached_conversions(client, auth_headers, variant_cache):
    image = uploaded(client, auth_headers)
    client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    assert uuid.UUID(image["id"]) in variant_cache.entries

    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204

    assert variant_cache.entries == {}
    webp = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    assert webp.status_code == 404


def test_conversions_and_delete_work_when_redis_is_down(client, auth_headers, closed_port):
    down = VariantCache(
        create_redis_client(f"redis://127.0.0.1:{closed_port}/0", 0.25),
        ttl_seconds=60,
        max_item_bytes=10**6,
    )
    app.dependency_overrides[get_variant_cache] = lambda: down
    image = uploaded(client, auth_headers)

    for _ in range(2):
        response = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
        assert response.status_code == 200
        assert decode(response.content).format == "WEBP"
    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204
    assert client.get(image["url"], headers=auth_headers).status_code == 404


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
    assert stored_keys(storage) == []


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


# --- Rate limiting --------------------------------------------------------------------------------

RATE_LIMIT_HEADERS = ("ratelimit", "ratelimit-policy", "retry-after")


def transform(client, headers, image_id, transformations=None):
    return client.post(
        f"/images/{image_id}/transform",
        headers=headers,
        json={"transformations": transformations or {"flip": True}},
    )


def test_transform_counts_the_caller_once_and_sends_rate_limit_headers(
    client, auth_headers, rate_limiter
):
    rate_limiter.decision = ALLOWED
    image = uploaded(client, auth_headers)

    response = transform(client, auth_headers, image["id"])

    assert response.status_code == 201
    assert response.headers["ratelimit-policy"] == ALLOWED.headers()["RateLimit-Policy"]
    assert response.headers["ratelimit"] == ALLOWED.headers()["RateLimit"]
    assert "retry-after" not in response.headers
    assert rate_limiter.hits == [user_id_of(auth_headers)]


def test_rate_limited_transform_is_429_and_creates_nothing(
    client, auth_headers, rate_limiter, storage
):
    image = uploaded(client, auth_headers)
    rate_limiter.decision = REJECTED

    response = transform(client, auth_headers, image["id"])

    assert response.status_code == 429
    assert response.json() == {
        "detail": "Too many transformations (limit: 30 per minute); retry in 13s"
    }
    assert response.headers["retry-after"] == "13"
    assert response.headers["ratelimit-policy"] == REJECTED.headers()["RateLimit-Policy"]
    assert response.headers["ratelimit"] == REJECTED.headers()["RateLimit"]
    assert client.get("/images", headers=auth_headers).json()["total"] == 1
    assert len(stored_keys(storage)) == 1


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("no token", 401),
        ("invalid token", 401),
        ("malformed JSON", 422),
        ("no transformations", 422),
        ("invalid transformation", 422),
        ("invalid image id", 422),
        ("unknown image", 404),
        ("another user's image", 404),
    ],
)
def test_limit_is_checked_after_auth_validation_and_ownership(
    client, auth_headers, rate_limiter, case, expected_status
):
    rate_limiter.decision = REJECTED
    image = uploaded(client, auth_headers)
    url = f"/images/{image['id']}/transform"
    request = {"headers": auth_headers, "json": {"transformations": {"flip": True}}}
    if case == "no token":
        request["headers"] = {}
    elif case == "invalid token":
        request["headers"] = {"Authorization": "Bearer not-a-token"}
    elif case == "malformed JSON":
        request = {
            "headers": {**auth_headers, "Content-Type": "application/json"},
            "content": b"{",
        }
    elif case == "no transformations":
        request["json"] = {"transformations": {}}
    elif case == "invalid transformation":
        request["json"] = {"transformations": {"rotate": 720}}
    elif case == "invalid image id":
        url = "/images/not-a-uuid/transform"
    elif case == "unknown image":
        url = f"/images/{uuid.uuid4()}/transform"
    elif case == "another user's image":
        request["headers"] = register(client, "bob")

    response = client.post(url, **request)

    assert response.status_code == expected_status
    if case == "malformed JSON":
        assert response.json()["detail"][0]["type"] == "json_invalid"
    assert rate_limiter.hits == []


def test_failed_transformation_still_counts(client, auth_headers, rate_limiter):
    image = uploaded(client, auth_headers, data=make_image_bytes(size=(100, 50)))

    response = transform(
        client, auth_headers, image["id"], {"crop": {"x": 90, "width": 50, "height": 10}}
    )

    assert response.status_code == 422
    assert len(rate_limiter.hits) == 1


def test_unchecked_transform_has_no_rate_limit_headers(client, auth_headers):
    image = uploaded(client, auth_headers)

    response = transform(client, auth_headers, image["id"])

    assert response.status_code == 201
    assert not set(RATE_LIMIT_HEADERS) & set(response.headers)


def test_transform_works_when_redis_is_down(client, auth_headers, closed_port, caplog):
    caplog.set_level(logging.WARNING, logger="app.ratelimit")
    limiter = RateLimiter(
        create_redis_client(f"redis://127.0.0.1:{closed_port}/0", 0.25), (Limit("minute", 1, 60),)
    )
    app.dependency_overrides[get_transform_rate_limiter] = lambda: limiter
    image = uploaded(client, auth_headers)

    for _ in range(2):
        response = transform(client, auth_headers, image["id"])
        assert response.status_code == 201
        assert not set(RATE_LIMIT_HEADERS) & set(response.headers)

    warnings = [record.getMessage() for record in caplog.records if record.name == "app.ratelimit"]
    assert len(warnings) == 1
    assert warnings[0].startswith("Transform rate limiter unavailable (ConnectionError")


def test_openapi_documents_rate_limit_response():
    responses = app.openapi()["paths"]["/images/{image_id}/transform"]["post"]["responses"]

    assert "Retry-After" in responses["429"]["headers"]
