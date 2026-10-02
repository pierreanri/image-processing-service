import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app
from app.routers.shared import MAX_AGE_SECONDS
from app.sharing import (
    ConversionSlots,
    ShareSigner,
    get_share_conversion_slots,
    get_share_signer,
)
from app.storage import CHUNK_SIZE, StorageUnavailableError
from tests.utils import make_image_bytes, register, share, stored_keys, user_id_of

NOW = 1_790_000_000.0
DAY = 24 * 60 * 60


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


@pytest.fixture
def signer(client, clock) -> ShareSigner:
    """The API's share-link signer, on a clock the test controls."""
    clock.now = NOW
    signer = ShareSigner(get_settings().jwt_secret, clock=clock)
    app.dependency_overrides[get_share_signer] = lambda: signer
    return signer


@pytest.fixture
def settings(client, monkeypatch):
    """The API's settings, which a test may change."""
    settings = get_settings().model_copy()
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


def upload(client, headers, data=None) -> dict:
    files = {"file": ("holiday.png", data or make_image_bytes(), "image/png")}
    response = client.post("/images", headers=headers, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def expires_at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1].split(".")[0]


def count_statements():
    """Counts the SQL statements run from here on: call the result to read the count."""
    statements = []
    engine = get_sessionmaker().kw["bind"]

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    return lambda: (event.remove(engine, "before_cursor_execute", count), len(statements))[1]


# --- Creating links -------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, {}])
def test_a_link_lasts_a_day_by_default(client, auth_headers, signer, body):
    image = upload(client, auth_headers)

    response = client.post(f"/images/{image['id']}/share-links", headers=auth_headers, json=body)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    link = response.json()
    assert link["url"].startswith("http://testserver/shared/")
    assert link["url"].endswith(".png")
    assert len(token_of(link["url"])) == 60
    assert link == {
        "url": link["url"],
        "image_id": image["id"],
        "format": "png",
        "mime_type": "image/png",
        "quality": None,
        "expires_at": expires_at(NOW + DAY),
    }


def test_a_link_can_convert_and_choose_its_lifetime(client, auth_headers, signer):
    image = upload(client, auth_headers)

    link = share(client, auth_headers, image["id"], format="WEBP", quality=70, expires_in=3600)

    assert link["url"].endswith(".webp")
    assert (link["format"], link["mime_type"], link["quality"]) == ("webp", "image/webp", 70)
    assert link["expires_at"] == expires_at(NOW + 3600)


def test_link_variants_are_normalized_like_downloads(client, auth_headers, signer):
    image = upload(client, auth_headers)
    original = share(client, auth_headers, image["id"])

    # PNG is lossless, so the quality is dropped and the link serves the stored file.
    assert share(client, auth_headers, image["id"], quality=10) == original
    assert share(client, auth_headers, image["id"], format="png") == original
    jpeg = share(client, auth_headers, image["id"], format="JPG")
    assert (jpeg["format"], jpeg["quality"]) == ("jpeg", None)
    assert jpeg["url"].endswith(".jpg")


def test_the_same_expiry_gives_the_same_url(client, auth_headers, signer):
    image = upload(client, auth_headers)
    pinned = expires_at(NOW + 2 * DAY)

    first = share(client, auth_headers, image["id"], expires_at=pinned)
    signer._clock.advance(600)  # a later request
    again = share(client, auth_headers, image["id"], expires_at=pinned.replace("Z", ".900Z"))

    assert again == first
    assert first["expires_at"] == pinned
    assert share(client, auth_headers, image["id"], expires_at=expires_at(NOW + DAY)) != first
    assert client.delete(f"/images/{image['id']}/share-links", headers=auth_headers).status_code
    assert share(client, auth_headers, image["id"], expires_at=pinned)["url"] != first["url"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"expires_in": 30 * DAY + 1}, "expires_in may be at most 2592000 seconds"),
        (
            {"expires_at": expires_at(NOW)},
            "expires_at must be in the future and at most 2592000 seconds away",
        ),
        (
            {"expires_at": expires_at(NOW - 60)},
            "expires_at must be in the future and at most 2592000 seconds away",
        ),
        (
            {"expires_at": expires_at(NOW + 30 * DAY + 1)},
            "expires_at must be in the future and at most 2592000 seconds away",
        ),
        ({"expires_in": 0}, None),
        ({"expires_at": "2026-10-02T12:00:00"}, None),  # no time zone
        ({"expires_in": 60, "expires_at": expires_at(NOW + 60)}, None),
        ({"format": "svg"}, None),
        ({"quality": 0}, None),
        ({"quality": 101}, None),
        ({"expire_in": 60}, None),
    ],
)
def test_invalid_link_requests_are_422(client, auth_headers, signer, body, detail):
    image = upload(client, auth_headers)

    response = client.post(f"/images/{image['id']}/share-links", headers=auth_headers, json=body)

    assert response.status_code == 422
    if detail:
        assert response.json() == {"detail": detail}


def test_the_longest_lifetime_is_configurable(client, auth_headers, signer, settings):
    settings.share_max_ttl_seconds = 3600
    image = upload(client, auth_headers)

    assert share(client, auth_headers, image["id"])["expires_at"] == expires_at(NOW + 3600)
    response = client.post(
        f"/images/{image['id']}/share-links", headers=auth_headers, json={"expires_in": 3601}
    )
    assert response.json() == {"detail": "expires_in may be at most 3600 seconds"}


@pytest.mark.parametrize("limit", ["max_dimension", "max_image_pixels"])
def test_conversion_links_downloads_could_never_serve_are_refused(
    client, auth_headers, signer, settings, limit
):
    image = upload(client, auth_headers)  # 64x48
    setattr(settings, limit, 32 if limit == "max_dimension" else 1000)

    response = client.post(
        f"/images/{image['id']}/share-links", headers=auth_headers, json={"format": "webp"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Image 64x48 is too large to convert; share it without format or quality"
    }
    # The stored file needs no conversion.
    assert share(client, auth_headers, image["id"])["format"] == "png"


def test_conversion_links_beyond_the_encoders_limit_are_refused(
    client, auth_headers, signer, settings
):
    settings.max_dimension = 20_000  # above WebP's limit of 16383 px
    image = upload(client, auth_headers, make_image_bytes(size=(16_384, 2)))

    response = client.post(
        f"/images/{image['id']}/share-links", headers=auth_headers, json={"format": "webp"}
    )

    assert response.status_code == 422
    assert share(client, auth_headers, image["id"], format="tiff")["format"] == "tiff"


def test_only_the_owner_can_create_or_revoke_links(client, auth_headers, signer):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"])
    bob = register(client, "bob")

    for method in ("post", "delete"):
        path = f"/images/{image['id']}/share-links"
        assert getattr(client, method)(path).status_code == 401
        bad_token = {"Authorization": "Bearer not-a-token"}
        assert getattr(client, method)(path, headers=bad_token).status_code == 401
        assert getattr(client, method)(path, headers=bob).status_code == 404
        unknown = f"/images/{uuid.uuid4()}/share-links"
        assert getattr(client, method)(unknown, headers=auth_headers).status_code == 404
        malformed = "/images/nope/share-links"
        assert getattr(client, method)(malformed, headers=auth_headers).status_code == 422

    assert client.get(link["url"]).status_code == 200  # bob's DELETE revoked nothing


# --- Downloading through links --------------------------------------------------------------------


def test_a_link_downloads_the_stored_file_without_a_token(client, auth_headers, signer):
    data = make_image_bytes("PNG")
    image = upload(client, auth_headers, data)
    link = share(client, auth_headers, image["id"])

    response = client.get(link["url"])

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(data))
    assert "transfer-encoding" not in response.headers
    owner_download = client.get(image["url"], headers=auth_headers)
    assert response.headers["etag"] == owner_download.headers["etag"]
    last_modified = parsedate_to_datetime(response.headers["last-modified"])
    assert abs((last_modified - datetime.fromisoformat(image["created_at"])).total_seconds()) < 1
    assert response.headers["cache-control"] == f"private, max-age={MAX_AGE_SECONDS}"
    assert response.headers["content-disposition"] == 'inline; filename="image.png"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    assert response.headers["x-robots-tag"] == "noindex"


def test_a_link_streams_large_files_intact(client, auth_headers, signer, storage):
    data = os.urandom(3 * CHUNK_SIZE + 1)
    image = upload(client, auth_headers)
    # Large random files aren't valid images, so replace the stored bytes directly.
    storage.save(stored_keys(storage)[0], data, content_type="image/png")
    link = share(client, auth_headers, image["id"])

    response = client.get(link["url"])

    assert response.content == data
    assert response.headers["content-length"] == str(len(data))


def test_a_conversion_link_shares_the_variant_cache(
    client, auth_headers, signer, storage, variant_cache
):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], format="webp", quality=70)

    response = client.get(link["url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    image_id = uuid.UUID(image["id"])
    assert response.headers["etag"] == f'"{image_id.hex}-webp-q70"'
    assert set(variant_cache.entries[image_id]) == {"webp-q70"}
    owner = client.get(image["url"], params={"format": "webp", "quality": 70}, headers=auth_headers)
    assert owner.content == response.content
    assert owner.headers["etag"] == response.headers["etag"]

    variant_cache.entries[image_id]["webp-q70"] = b"cached bytes"
    storage.read = lambda key: pytest.fail("a cached variant must not be converted again")
    assert client.get(link["url"]).content == b"cached bytes"


def test_the_query_string_is_ignored(client, auth_headers, signer, variant_cache):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], format="webp")

    response = client.get(link["url"], params={"format": "png", "quality": 1, "fbclid": "x"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert set(variant_cache.entries[uuid.UUID(image["id"])]) == {"webp-qdefault"}


@pytest.mark.parametrize("format", [None, "webp"])
def test_links_support_conditional_requests(client, auth_headers, signer, variant_cache, format):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], **({"format": format} if format else {}))
    etag = client.get(link["url"]).headers["etag"]
    variant_cache.reads.clear()

    response = client.get(link["url"], headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"].startswith("private, max-age=")
    assert response.headers["access-control-allow-origin"] == "*"
    assert variant_cache.reads == []


def test_browsers_never_keep_a_download_longer_than_its_link(client, auth_headers, signer):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], expires_in=600)

    assert client.get(link["url"]).headers["cache-control"] == "private, max-age=600"
    signer._clock.advance(590.5)
    assert client.get(link["url"]).headers["cache-control"] == "private, max-age=9"


def test_a_link_expires_at_its_expiry_second(client, auth_headers, signer):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], expires_in=600)
    etag = client.get(link["url"]).headers["etag"]

    signer._clock.advance(599.9)
    assert client.get(link["url"]).status_code == 200
    signer._clock.advance(0.1)

    for headers in ({}, {"If-None-Match": etag}, {"If-None-Match": "*"}):
        response = client.get(link["url"], headers=headers)
        assert response.status_code == 410
        assert response.json() == {"detail": f"Share link expired at {expires_at(NOW + 600)}"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == "*"
        assert "etag" not in response.headers


def test_revoking_ends_the_links_made_before(client, auth_headers, signer):
    image = upload(client, auth_headers)
    original = share(client, auth_headers, image["id"])
    converted = share(client, auth_headers, image["id"], format="webp")
    etag = client.get(original["url"]).headers["etag"]
    other_image = upload(client, auth_headers)
    other = share(client, auth_headers, other_image["id"])

    response = client.delete(f"/images/{image['id']}/share-links", headers=auth_headers)

    assert response.status_code == 204
    for url, headers in (
        (original["url"], {}),
        (converted["url"], {}),
        (original["url"], {"If-None-Match": etag}),
    ):
        revoked = client.get(url, headers=headers)
        assert revoked.status_code == 410
        assert revoked.json() == {"detail": "Share link has been revoked"}
        assert revoked.headers["cache-control"] == "no-store"
    assert client.get(other["url"]).status_code == 200  # per image
    # The owner still downloads it, and new links work...
    assert client.get(image["url"], headers=auth_headers).status_code == 200
    later = share(client, auth_headers, image["id"])
    assert client.get(later["url"]).status_code == 200
    # ...until the next revocation (revoking again is fine).
    for _ in range(2):
        response = client.delete(f"/images/{image['id']}/share-links", headers=auth_headers)
        assert response.status_code == 204
    assert client.get(later["url"]).status_code == 410


def test_deleting_the_image_ends_its_links(client, auth_headers, signer, variant_cache):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], format="webp")
    client.get(link["url"])
    other = upload(client, auth_headers)
    revoked = share(client, auth_headers, other["id"])
    client.delete(f"/images/{other['id']}/share-links", headers=auth_headers)

    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204

    response = client.get(link["url"])
    assert response.status_code == 410
    # Exactly as a revoked link: a link never tells whether its image still exists.
    assert response.json() == client.get(revoked["url"]).json()
    assert uuid.UUID(image["id"]) not in variant_cache.entries


def test_links_to_transformed_images_work(client, auth_headers, signer, worker):
    source = upload(client, auth_headers)
    derived = client.post(
        f"/images/{source['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"rotate": 90}},
    ).json()
    job = client.post(
        f"/images/{source['id']}/transform",
        headers={**auth_headers, "Prefer": "respond-async"},
        json={"transformations": {"flip": True}},
    ).json()
    worker.run_once()
    from_job = client.get(job["url"], headers=auth_headers).json()["result"]
    links = [share(client, auth_headers, image["id"]) for image in (derived, from_job)]

    client.delete(f"/images/{source['id']}", headers=auth_headers)

    for link in links:
        assert client.get(link["url"]).status_code == 200


def forged_urls(url: str, signer: ShareSigner) -> dict[str, str]:
    """Variations of a genuine link's URL that this service never issued."""
    base, name = url.rsplit("/", 1)
    token, ext = name.split(".")

    def changed(char: str) -> str:
        return "B" if char == "A" else "A"

    other_signer = ShareSigner("another-secret-that-is-at-least-32-characters")
    return {
        "payload": f"{base}/{token[:5]}{changed(token[5])}{token[6:]}.{ext}",
        "tag": f"{base}/{token[:-1]}{changed(token[-1])}.{ext}",
        "short": f"{base}/{token[:-1]}.{ext}",
        "long": f"{base}/{token}A.{ext}",
        "extension": f"{base}/{token}.{'jpg' if ext != 'jpg' else 'png'}",
        "unknown-extension": f"{base}/{token}.svg",
        "padding": f"{base}/{token[:-1]}=.{ext}",
        "bang": f"{base}/{token[:-1]}!.{ext}",
        "other-secret": f"{base}/{other_signer.sign(signer.verify(token))}.{ext}",
    }


def test_links_this_service_did_not_issue_get_one_404(
    client, auth_headers, signer, storage, variant_cache
):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], format="webp")
    storage.open = storage.read = lambda key: pytest.fail("storage must not be touched")
    variant_cache.reads.clear()
    statements = count_statements()

    responses = {
        name: client.get(url, headers={"If-None-Match": "*"})
        for name, url in forged_urls(link["url"], signer).items()
    }

    assert statements() == 0
    assert variant_cache.reads == []
    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.json() == {"detail": "Share link not found"}, name
        headers = {k: v for k, v in response.headers.items() if k != "date"}
        assert headers == {
            "cache-control": "no-store",
            "access-control-allow-origin": "*",
            "content-type": "application/json",
            "content-length": str(len(response.content)),
        }, name


def test_a_link_grants_nothing_else_and_tells_nothing_about_its_owner(
    client, auth_headers, signer, storage
):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"])
    bob = register(client, "bob")
    owner = user_id_of(auth_headers)

    response = client.get(link["url"], headers=bob)  # bob's token is ignored

    assert response.status_code == 200
    everything = str(response.headers).lower()
    for secret in (str(owner), owner.hex, "alice", "holiday", stored_keys(storage)[0]):
        assert secret.lower() not in everything
    # Holding the link gives bob no access to the image through the API.
    assert client.get(f"/images/{image['id']}", headers=bob).status_code == 404
    assert client.get(image["url"], headers=bob).status_code == 404
    # And a link is not a bearer token.
    as_bearer = {"Authorization": f"Bearer {token_of(link['url'])}"}
    rejected = client.get(f"/images/{image['id']}", headers=as_bearer)
    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "path",
    [
        "/shared/{token}",  # cut before its extension
        "/shared/{token}.",
        "/shared/{token}.webp/",
        "/shared/Afy3_PoR_EZ-leX7kFw7Mz",  # cut inside the token
        "/shared/",
        "/shared/a/b/c",
    ],
)
def test_other_paths_under_shared_get_the_same_404(client, auth_headers, signer, path):
    image = upload(client, auth_headers)
    token = token_of(share(client, auth_headers, image["id"], format="webp")["url"])
    statements = count_statements()

    response = client.get(path.format(token=token))

    assert statements() == 0
    assert response.status_code == 404
    assert response.json() == {"detail": "Share link not found"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"


def test_an_invalid_authorization_header_is_ignored(client, auth_headers, signer):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"])

    response = client.get(link["url"], headers={"Authorization": "Bearer nonsense"})

    assert response.status_code == 200


# --- Conversion slots -----------------------------------------------------------------------------


@pytest.fixture
def busy_slots(client):
    """Conversions through links have one slot, taken for the whole test, and nobody may wait."""
    slots = ConversionSlots(1, max_waiting=0)
    app.dependency_overrides[get_share_conversion_slots] = lambda: slots
    with slots.hold():
        yield slots


def test_link_conversions_are_turned_away_when_every_slot_is_busy(
    client, auth_headers, signer, busy_slots, variant_cache
):
    image = upload(client, auth_headers)
    converted = share(client, auth_headers, image["id"], format="webp")
    cached = share(client, auth_headers, image["id"], format="jpeg")
    variant_cache.set(uuid.UUID(image["id"]), "jpeg-qdefault", b"cached jpeg")
    original = share(client, auth_headers, image["id"])

    response = client.get(converted["url"])

    assert response.status_code == 503
    assert response.json() == {"detail": "Too many conversions in progress; retry shortly"}
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"
    # Only conversions through links need a slot.
    assert client.get(original["url"]).status_code == 200
    assert client.get(cached["url"]).content == b"cached jpeg"
    etag = f'"{uuid.UUID(image["id"]).hex}-webp-qdefault"'
    assert client.get(converted["url"], headers={"If-None-Match": etag}).status_code == 304
    owner = client.get(image["url"], params={"format": "webp"}, headers=auth_headers)
    assert owner.status_code == 200


def test_a_conversion_that_waited_uses_what_was_cached_meanwhile(
    client, auth_headers, signer, storage, variant_cache
):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], format="webp")
    image_id = uuid.UUID(image["id"])

    class FinishesAnotherConversionFirst(ConversionSlots):
        def hold(self):
            # Another request converted the same variant while this one waited.
            variant_cache.set(image_id, "webp-qdefault", b"converted meanwhile")
            return super().hold()

    app.dependency_overrides[get_share_conversion_slots] = lambda: FinishesAnotherConversionFirst(1)
    storage.read = lambda key: pytest.fail("the variant must not be converted again")

    assert client.get(link["url"]).content == b"converted meanwhile"


# --- Failures -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("format", "method"), [(None, "open"), ("webp", "read")])
def test_a_storage_outage_is_503_and_the_log_hides_the_link(
    client, auth_headers, signer, storage, caplog, format, method
):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], **({"format": format} if format else {}))

    def unavailable(*args, **kwargs):
        raise StorageUnavailableError("down")

    setattr(storage, method, unavailable)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = client.get(link["url"])

    assert response.status_code == 503
    assert response.json() == {"detail": "Image storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"
    [record] = [record for record in caplog.records if record.name == "app.main"]
    token = token_of(link["url"])
    assert f"/shared/{token[:36]}[redacted]" in record.getMessage()
    assert token[36:] not in record.getMessage()


def test_head_is_not_supported(client, auth_headers, signer):
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"])

    assert client.head(link["url"]).status_code == 405


def test_openapi_documents_share_links():
    paths = app.openapi()["paths"]
    download = paths["/shared/{token}.{ext}"]["get"]

    assert "security" not in download
    assert {"200", "304", "404", "410", "503"} <= set(download["responses"])
    links = paths["/images/{image_id}/share-links"]
    assert set(links) == {"post", "delete"}
    assert all(operation["security"] for operation in links.values())
    assert links["post"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ShareLinkOut")


def test_links_expire_by_the_server_clock_not_the_link(client, auth_headers, signer):
    """A link's expiry can't be pushed back: it is inside the signed token."""
    image = upload(client, auth_headers)
    link = share(client, auth_headers, image["id"], expires_in=60)
    assert signer.verify(token_of(link["url"])).expires == NOW + 60

    signer._clock.advance(timedelta(days=1).total_seconds())

    assert client.get(link["url"]).status_code == 410
