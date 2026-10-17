"""Downloads through signed URLs (GET /signed/{key_id}/{signature}/[{options}/]{image_id}.{ext})."""

import io
import logging
import math
import os
import uuid
from datetime import UTC, datetime

import pytest
from PIL import Image

from app.cache import VariantCache, get_variant_cache
from app.config import get_settings
from app.main import app
from app.ratelimit import (
    RENDER_KEY_PREFIX,
    Limit,
    RateLimiter,
    build_render_rate_limiter,
    get_render_rate_limiter,
)
from app.redis_client import create_redis_client
from app.routers.downloads import transformation_variant
from app.routers.signed import MAX_AGE_SECONDS, SHARED_MAX_AGE_SECONDS
from app.sharing import ConversionSlots, get_share_conversion_slots
from app.signed_urls import UrlSigner, get_signed_render_slots, get_url_signer, parse_options, sign
from app.storage import CHUNK_SIZE, StorageUnavailableError
from tests.utils import (
    REJECTED,
    FakeRateLimiter,
    count_statements,
    create_url_signing_key,
    make_image_bytes,
    register,
    share,
    stored_keys,
    user_id_of,
)

NOW = 1_790_000_000.0
DAY = 24 * 60 * 60
NOT_FOUND = {"detail": "Signed URL not found"}
REVOKED = {"detail": "Signed URL has been revoked"}
CACHED = f"public, max-age={MAX_AGE_SECONDS}, s-maxage={SHARED_MAX_AGE_SECONDS}, immutable"


@pytest.fixture(params=["local", "s3"])
def storage(request):
    """Every test in this module runs against both storage backends (S3 is moto's in-memory S3)."""
    return request.getfixturevalue(f"{request.param}_storage")


@pytest.fixture
def signer(client, clock) -> UrlSigner:
    """The API's URL signer, on a clock the test controls."""
    clock.now = NOW
    signer = UrlSigner(get_settings().jwt_secret, clock=clock)
    app.dependency_overrides[get_url_signer] = lambda: signer
    return signer


@pytest.fixture
def key(client, auth_headers, signer) -> dict:
    """Alice's URL signing key, as POST /me/url-signing-key shows it."""
    return create_url_signing_key(client, auth_headers)


@pytest.fixture
def settings(client):
    """The API's settings, which a test may change."""
    settings = get_settings().model_copy()
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@pytest.fixture
def render_slots(client) -> ConversionSlots:
    """Signed URLs' render slots: one, and nobody may wait for it."""
    slots = ConversionSlots(1, max_waiting=0)
    app.dependency_overrides[get_signed_render_slots] = lambda: slots
    return slots


def signed_url(key: dict, image_id: str, extension: str, options: str | None = None) -> str:
    """A URL signed with `key` (as the key API returns it), as the account's backend builds it."""
    path = f"{image_id}.{extension}" if options is None else f"{options}/{image_id}.{extension}"
    return f"{key['url_prefix']}{sign(key['key'], path)}/{path}"


def make_photo(size: tuple[int, int] = (64, 48)) -> bytes:
    """A PNG whose pixels differ, so that every transformation changes the bytes."""
    width, height = size
    image = Image.new("RGB", size)
    image.putdata(
        [
            (x * 255 // width, y * 255 // height, (x * y) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def pixels(data: bytes) -> tuple:
    with Image.open(io.BytesIO(data)) as image:
        return image.mode, image.size, image.tobytes()


def upload(client, headers, data=None) -> dict:
    files = {"file": ("holiday.png", data or make_photo(), "image/png")}
    response = client.post("/images", headers=headers, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def expires_at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def without_date(response) -> dict:
    return {name: value for name, value in response.headers.items() if name != "date"}


def no_storage(storage) -> None:
    storage.open = storage.read = lambda key: pytest.fail("storage must not be touched")


class NoSlots(ConversionSlots):
    def hold(self, *args):
        pytest.fail("no render slot may be taken")


# --- What signed URLs serve -----------------------------------------------------------------------

MATRIX = [
    ("w_32", "png", {"resize": {"width": 32}}),
    (
        "crop_8_4_40_30,w_20,h_20,c_cover",
        "jpg",
        {
            "crop": {"x": 8, "y": 4, "width": 40, "height": 30},
            "resize": {"width": 20, "height": 20, "fit": "cover"},
            "format": "jpeg",
        },
    ),
    (
        "w_40,h_10,c_contain,a_45,mirror",
        "webp",
        {
            "resize": {"width": 40, "height": 10, "fit": "contain"},
            "rotate": 45,
            "mirror": True,
            "format": "webp",
        },
    ),
    (
        "h_30,a_270,flip,blur_1.5,sharpen,grayscale",
        "png",
        {
            "resize": {"height": 30},
            "rotate": -90,
            "flip": True,
            "filters": {"blur": 1.5, "sharpen": True, "grayscale": True},
        },
    ),
    (
        "sepia,wm_SGkgdGhlcmU,g_center,o_80,fs_12,q_60",
        "jpg",
        {
            "filters": {"sepia": True},
            "watermark": {"text": "Hi there", "position": "center", "opacity": 0.8, "size": 12},
            "quality": 60,
            "format": "jpeg",
        },
    ),
    ("w_50,h_20", "gif", {"resize": {"width": 50, "height": 20}, "format": "gif"}),
    (
        "a_12.5,wm_8J-QkSBvaw",
        "tiff",
        {"rotate": 12.5, "watermark": {"text": "🐑 ok"}, "format": "tiff"},
    ),
    ("q_35", "webp", {"format": "webp", "quality": 35}),
    (None, "bmp", {"format": "bmp"}),
]


@pytest.mark.parametrize(("options", "extension", "transformations"), MATRIX)
def test_a_signed_url_serves_what_post_transform_makes(
    client, auth_headers, signer, key, options, extension, transformations
):
    image = upload(client, auth_headers)
    derived = client.post(
        f"/images/{image['id']}/transform",
        headers=auth_headers,
        json={"transformations": transformations},
    ).json()
    expected = client.get(derived["url"], headers=auth_headers).content

    response = client.get(signed_url(key, image["id"], extension, options))

    assert response.status_code == 200, response.text
    if derived["format"] == "png":
        # The same pixels, compressed faster than a stored image is.
        assert pixels(response.content) == pixels(expected)
        assert response.content != expected
    else:
        assert response.content == expected
    assert response.headers["content-type"] == derived["mime_type"]
    assert response.headers["content-length"] == str(len(response.content))
    parsed = parse_options(options, derived["format"])
    token = transformation_variant("png", parsed.spec, parsed.transformations).token
    assert response.headers["etag"] == f'"{uuid.UUID(image["id"]).hex}-{token}"'


def test_a_signed_url_sends_exactly_these_headers(client, auth_headers, signer, key):
    image = upload(client, auth_headers)

    response = client.get(signed_url(key, image["id"], "webp", "w_32"))

    assert response.status_code == 200
    assert without_date(response) == {
        "content-type": "image/webp",
        "content-length": str(len(response.content)),
        "etag": response.headers["etag"],
        "cache-control": CACHED,
        "access-control-allow-origin": "*",
        "cross-origin-resource-policy": "cross-origin",
        "x-content-type-options": "nosniff",
    }


def test_a_signed_url_without_options_streams_the_stored_file(
    client, auth_headers, signer, key, render_rate_limiter
):
    data = make_photo()
    image = upload(client, auth_headers, data)

    response = client.get(signed_url(key, image["id"], "png"))

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(data))
    assert "transfer-encoding" not in response.headers
    assert (
        response.headers["etag"] == client.get(image["url"], headers=auth_headers).headers["etag"]
    )
    assert response.headers["last-modified"]
    assert response.headers["cache-control"] == CACHED
    assert render_rate_limiter.checks == [] and render_rate_limiter.hits == []


def test_a_signed_url_streams_large_files_intact(client, auth_headers, signer, key, storage):
    data = os.urandom(3 * CHUNK_SIZE + 1)
    image = upload(client, auth_headers)
    # Large random files aren't valid images, so replace the stored bytes directly.
    storage.save(stored_keys(storage)[0], data, content_type="image/png")

    response = client.get(signed_url(key, image["id"], "png"))

    assert response.content == data
    assert response.headers["content-length"] == str(len(data))


def test_conversions_share_the_cache_and_etags_of_downloads_and_share_links(
    client, auth_headers, signer, key, storage, variant_cache
):
    image = upload(client, auth_headers)
    image_id = uuid.UUID(image["id"])

    response = client.get(signed_url(key, image["id"], "webp", "q_70"))

    assert response.status_code == 200
    assert response.headers["etag"] == f'"{image_id.hex}-webp-q70"'
    assert set(variant_cache.entries[image_id]) == {"webp-q70"}
    owner = client.get(image["url"], params={"format": "webp", "quality": 70}, headers=auth_headers)
    link = client.get(share(client, auth_headers, image["id"], format="webp", quality=70)["url"])
    for other in (owner, link):
        assert other.content == response.content
        assert other.headers["etag"] == response.headers["etag"]

    variant_cache.entries[image_id]["webp-q70"] = b"cached bytes"
    no_storage(storage)
    assert client.get(signed_url(key, image["id"], "webp", "q_70")).content == b"cached bytes"


def test_transformations_are_rendered_once_then_served_from_the_cache(
    client, auth_headers, signer, key, storage, variant_cache, render_rate_limiter
):
    image = upload(client, auth_headers)
    image_id = uuid.UUID(image["id"])
    url = signed_url(key, image["id"], "png", "w_32,grayscale")

    rendered = client.get(url)

    [token] = variant_cache.entries[image_id]
    assert token.startswith("png-t") and len(token) == len("png-t") + 32
    assert variant_cache.entries[image_id][token] == rendered.content
    assert render_rate_limiter.hits == [user_id_of(auth_headers)]

    no_storage(storage)
    again = client.get(url)
    # The expiry is not part of the variant: an expiring URL to it is served from the cache too.
    expiring = client.get(
        signed_url(key, image["id"], "png", f"w_32,grayscale,exp_{NOW + DAY:.0f}")
    )

    for response in (again, expiring):
        assert response.status_code == 200
        assert response.content == rendered.content
        assert response.headers["etag"] == rendered.headers["etag"]
    assert render_rate_limiter.hits == [user_id_of(auth_headers)]  # cache hits cost nothing


@pytest.mark.parametrize(
    ("options", "extension"),
    [(None, "png"), (None, "webp"), ("q_70", "webp"), ("w_32", "png"), ("w_32", "webp")],
)
def test_signed_urls_support_conditional_requests(
    client,
    auth_headers,
    signer,
    key,
    storage,
    variant_cache,
    render_rate_limiter,
    options,
    extension,
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], extension, options)
    etag = client.get(url).headers["etag"]
    variant_cache.reads.clear()
    render_rate_limiter.checks.clear()
    render_rate_limiter.hits.clear()
    no_storage(storage)
    statements = count_statements()

    response = client.get(url, headers={"If-None-Match": f'"other", {etag}'})

    assert statements() == 1  # the image and the key, in one
    assert response.status_code == 304
    assert response.content == b""
    assert without_date(response) == {
        "etag": etag,
        "cache-control": CACHED,
        "access-control-allow-origin": "*",
        "cross-origin-resource-policy": "cross-origin",
        "x-content-type-options": "nosniff",
    }
    assert variant_cache.reads == []
    assert render_rate_limiter.checks == [] and render_rate_limiter.hits == []


def test_a_signed_url_grants_nothing_else_and_tells_nothing_about_its_owner(
    client, auth_headers, signer, key, storage
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "png")
    bob = register(client, "bob")
    owner = user_id_of(auth_headers)

    for headers in ({"Authorization": "Bearer nonsense"}, bob):  # tokens are ignored
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        everything = str(response.headers).lower()
        for secret in (str(owner), owner.hex, "alice", "holiday", stored_keys(storage)[0]):
            assert secret.lower() not in everything
    # The key is not a bearer token.
    as_bearer = {"Authorization": f"Bearer {key['key']}"}
    assert client.get(f"/images/{image['id']}", headers=as_bearer).status_code == 401
    assert client.get(image["url"], headers=bob).status_code == 404


def test_signed_urls_to_transformed_images_work(client, auth_headers, signer, key):
    source = upload(client, auth_headers)
    derived = client.post(
        f"/images/{source['id']}/transform",
        headers=auth_headers,
        json={"transformations": {"rotate": 90}},
    ).json()

    client.delete(f"/images/{source['id']}", headers=auth_headers)

    response = client.get(signed_url(key, derived["id"], "webp", "w_16"))
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size == (16, 21)


# --- Expiry ---------------------------------------------------------------------------------------


def test_caches_never_keep_an_expiring_url_longer_than_it_lasts(client, auth_headers, signer, key):
    image = upload(client, auth_headers)
    soon = signed_url(key, image["id"], "webp", f"exp_{NOW + 600:.0f}")
    later = signed_url(key, image["id"], "webp", f"exp_{NOW + 2 * DAY:.0f}")
    much_later = signed_url(key, image["id"], "webp", f"exp_{NOW + 90 * DAY:.0f}")

    response = client.get(soon)
    assert response.headers["cache-control"] == "public, max-age=600, s-maxage=600, immutable"
    # A search index would keep the URL past its expiry.
    assert response.headers["x-robots-tag"] == "noindex"
    assert client.get(later).headers["cache-control"] == (
        f"public, max-age={2 * DAY}, s-maxage={SHARED_MAX_AGE_SECONDS}, immutable"
    )
    assert client.get(much_later).headers["cache-control"] == CACHED
    signer._clock.advance(590.5)
    assert client.get(soon).headers["cache-control"] == "public, max-age=9, s-maxage=9, immutable"


def test_a_signed_url_expires_at_its_expiry_second(client, auth_headers, signer, key):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", f"w_32,exp_{NOW + 600:.0f}")
    etag = client.get(url).headers["etag"]

    signer._clock.advance(599.9)
    assert client.get(url).status_code == 200
    signer._clock.advance(0.1)

    for headers in ({}, {"If-None-Match": etag}, {"If-None-Match": "*"}):
        statements = count_statements()
        response = client.get(url, headers=headers)
        assert statements() == 0
        assert response.status_code == 410
        assert response.json() == {"detail": f"Signed URL expired at {expires_at(NOW + 600)}"}
        # It stays expired, so caches may keep the answer.
        assert without_date(response) == {
            "cache-control": "public, max-age=3600",
            "access-control-allow-origin": "*",
            "content-type": "application/json",
            "content-length": str(len(response.content)),
        }


# --- Revocation -----------------------------------------------------------------------------------


def test_rotating_the_key_ends_every_url_it_signed(client, auth_headers, signer, key):
    image = upload(client, auth_headers)
    stored = signed_url(key, image["id"], "png")
    rendered = signed_url(key, image["id"], "png", "w_32")
    etag = client.get(rendered).headers["etag"]

    rotated = client.post("/me/url-signing-key/rotate", headers=auth_headers).json()

    for url, headers in ((stored, {}), (rendered, {}), (rendered, {"If-None-Match": etag})):
        response = client.get(url, headers=headers)
        assert response.status_code == 410
        assert response.json() == REVOKED
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert response.headers["access-control-allow-origin"] == "*"
        assert "etag" not in response.headers
    new = signed_url(rotated, image["id"], "png", "w_32")
    assert client.get(new).status_code == 200

    # Deleting the key ends its URLs too, and a new key brings none of the old ones back.
    assert client.delete("/me/url-signing-key", headers=auth_headers).status_code == 204
    assert client.get(new).json() == REVOKED
    again = create_url_signing_key(client, auth_headers)
    for url in (stored, rendered, new):
        assert client.get(url).json() == REVOKED
    assert client.get(signed_url(again, image["id"], "png", "w_32")).status_code == 200


def test_a_key_signs_urls_to_its_own_accounts_images_only(client, auth_headers, signer, key):
    image = upload(client, auth_headers)
    bob = register(client, "bob")
    bob_key = create_url_signing_key(client, bob)
    bob_image = upload(client, bob)

    for url in (
        signed_url(bob_key, image["id"], "png"),  # alice's image, bob's key
        signed_url(key, bob_image["id"], "png"),  # and the other way round
        signed_url(key, str(uuid.uuid4()), "png"),  # no such image
    ):
        response = client.get(url)
        assert response.status_code == 410
        assert response.json() == REVOKED
    assert client.get(signed_url(bob_key, bob_image["id"], "png")).status_code == 200


def test_deleting_the_image_ends_its_urls_and_drops_its_variants(
    client, auth_headers, signer, key, variant_cache
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", "w_32")
    assert client.get(url).status_code == 200

    assert client.delete(f"/images/{image['id']}", headers=auth_headers).status_code == 204

    response = client.get(url)
    assert response.status_code == 410
    # Exactly as a rotated key: a URL never tells whether its image still exists.
    assert response.json() == REVOKED
    assert uuid.UUID(image["id"]) not in variant_cache.entries


# --- URLs this service did not sign ---------------------------------------------------------------


def forged_urls(key: dict, image_id: str, other_image_id: str) -> dict[str, str]:
    """Variations of a genuine signed URL that its key never signed."""
    url = signed_url(key, image_id, "webp", f"w_32,exp_{NOW + DAY:.0f}")
    prefix, signature, options, name = url.rsplit("/", 3)
    base = prefix.rsplit("/", 1)[0]
    key_id = key["key_id"]

    def changed(text: str, position: int = 0) -> str:
        return text[:position] + ("B" if text[position] == "A" else "A") + text[position + 1 :]

    other_secret = UrlSigner("another-secret-that-is-at-least-32-characters")
    other_key = {**key, "key": other_secret.key(key_id)}
    return {
        "signature": f"{prefix}/{changed(signature, 5)}/{options}/{name}",
        "short-signature": f"{prefix}/{signature[:-1]}/{options}/{name}",
        "long-signature": f"{prefix}/{signature}A/{options}/{name}",
        "key-id": f"{base}/{changed(key_id)}/{signature}/{options}/{name}",
        "short-key-id": f"{base}/{key_id[:-1]}/{signature}/{options}/{name}",
        "options": f"{prefix}/{signature}/w_33,exp_{NOW + DAY:.0f}/{name}",
        "expiry": f"{prefix}/{signature}/w_32,exp_{NOW + 2 * DAY:.0f}/{name}",
        "no-options": f"{prefix}/{signature}/{name}",
        "extension": f"{prefix}/{signature}/{options}/{image_id}.png",
        "unknown-extension": f"{prefix}/{signature}/{options}/{image_id}.svg",
        "jpeg-extension": f"{prefix}/{signature}/{options}/{image_id}.jpeg",
        "image": f"{prefix}/{signature}/{options}/{other_image_id}.webp",
        "uppercase-image-id": f"{prefix}/{signature}/{options}/{image_id.upper()}.webp",
        "trailing-slash": f"{url}/",
        "extra-segment": f"{prefix}/{signature}/{options}/w_32/{name}",
        "padding": f"{prefix}/{signature[:-1]}=/{options}/{name}",
        "other-secret": signed_url(other_key, image_id, "webp", f"w_32,exp_{NOW + DAY:.0f}"),
    }


def test_urls_this_service_did_not_sign_get_one_404_without_any_io(
    client, auth_headers, signer, key, storage, variant_cache, render_rate_limiter
):
    image = upload(client, auth_headers)
    other = upload(client, auth_headers)
    no_storage(storage)
    variant_cache.reads.clear()
    statements = count_statements()

    responses = {
        name: client.get(url, headers={"If-None-Match": "*"})
        for name, url in forged_urls(key, image["id"], other["id"]).items()
    }

    assert statements() == 0
    assert variant_cache.reads == []
    assert render_rate_limiter.checks == [] and render_rate_limiter.hits == []
    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.json() == NOT_FOUND, name
        assert without_date(response) == {
            "cache-control": "no-store",
            "access-control-allow-origin": "*",
            "content-type": "application/json",
            "content-length": str(len(response.content)),
        }, name


def test_a_signed_url_must_be_sent_exactly_as_signed(
    client, auth_headers, signer, key, storage, variant_cache
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", "w_32,h_20")
    assert client.get(url).status_code == 200
    no_storage(storage)
    variant_cache.reads.clear()
    statements = count_statements()

    responses = [
        client.get(respelled)
        for respelled in (
            url.replace(",", "%2C"),
            url.replace("w_32", "w%5F32"),
            url.replace(".webp", "%2Ewebp"),
            url.replace("/w_32", "%2Fw_32"),
            url + "?v=2",
            url + "?w_32",
        )
    ]

    assert statements() == 0
    assert variant_cache.reads == []
    for response in responses:
        assert response.status_code == 404, response.url
        assert response.json() == {
            "detail": "Signed URL not found: send it exactly as signed, without percent-encoding "
            "or a query string"
        }
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    "path",
    [
        "/signed/",
        "/signed/{key_id}",
        "/signed/{key_id}/",
        "/signed/{key_id}/{signature}",
        "/signed/{key_id}/{signature}/",
        "/signed/a/b/c/d/e/f",
    ],
)
def test_other_paths_under_signed_get_the_same_404(client, auth_headers, signer, key, path):
    image = upload(client, auth_headers)
    signature = signed_url(key, image["id"], "png").rsplit("/", 2)[1]
    statements = count_statements()

    response = client.get(path.format(key_id=key["key_id"], signature=signature))

    assert statements() == 0
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    ("options", "extension", "reason"),
    [
        ("h_20,w_40", "webp", 'not in canonical form; sign "w_40,h_20" instead'),
        ("w_040", "webp", 'not in canonical form; sign "w_40" instead'),
        ("w_40,h_20,c_fill", "webp", 'not in canonical form; sign "w_40,h_20" instead'),
        ("a_-90", "webp", 'not in canonical form; sign "a_270" instead'),
        (
            "q_80",
            "png",
            "not in canonical form: these options change nothing, leave the options segment out",
        ),
        ("w_0", "webp", "w_: Input should be greater than 0"),
        ("f_webp", "png", "there is no f_: the format is the file extension (…/{image id}.webp)"),
        ("w_32,w_32", "webp", "w is given twice"),
    ],
)
def test_correctly_signed_but_invalid_options_are_422(
    client, auth_headers, signer, key, variant_cache, options, extension, reason
):
    image = upload(client, auth_headers)
    statements = count_statements()

    response = client.get(signed_url(key, image["id"], extension, options))

    assert statements() == 0
    assert response.status_code == 422
    assert response.json() == {"detail": f"Invalid signed URL options: {reason}"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"
    assert variant_cache.reads == []


# --- Rendering errors -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "transformations"),
    [
        ("crop_40_40_30_30", {"crop": {"x": 40, "y": 40, "width": 30, "height": 30}}),
        ("w_20000", {"resize": {"width": 20_000}}),
        ("w_9000,a_45", {"resize": {"width": 9000}, "rotate": 45}),
        (
            "w_9999,h_9999,c_cover,a_30",
            {"resize": {"width": 9999, "height": 9999, "fit": "cover"}, "rotate": 30},
        ),
    ],
)
def test_what_cannot_be_rendered_is_refused_before_reading_the_image(
    client, auth_headers, signer, key, storage, render_rate_limiter, options, transformations
):
    image = upload(client, auth_headers)
    refused = client.post(
        f"/images/{image['id']}/transform",
        headers=auth_headers,
        json={"transformations": transformations},
    )
    assert refused.status_code == 422
    no_storage(storage)
    app.dependency_overrides[get_signed_render_slots] = lambda: NoSlots(1)

    response = client.get(signed_url(key, image["id"], "png", options))

    assert response.status_code == 422
    assert response.json() == refused.json()  # the same reason as POST /transform gives
    # The same URL fails the same way: caches may keep the answer a little while.
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["access-control-allow-origin"] == "*"
    assert render_rate_limiter.checks == [] and render_rate_limiter.hits == []


def test_images_too_large_to_render_are_refused_and_briefly_cached(
    client, auth_headers, signer, key, settings
):
    image = upload(client, auth_headers)
    settings.max_image_pixels = 1000  # lowered since the upload

    response = client.get(signed_url(key, image["id"], "webp", "w_32"))

    assert response.status_code == 400
    assert response.json() == {"detail": "Image is too large (64x48 pixels)"}
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["access-control-allow-origin"] == "*"
    # The stored file needs no rendering.
    assert client.get(signed_url(key, image["id"], "png")).status_code == 200


# --- Render slots ---------------------------------------------------------------------------------


def test_renders_are_turned_away_when_every_slot_is_busy(
    client, auth_headers, signer, key, render_slots, variant_cache
):
    image = upload(client, auth_headers)
    image_id = uuid.UUID(image["id"])
    rendered = signed_url(key, image["id"], "webp", "w_32")
    etag = client.get(rendered).headers["etag"]
    variant_cache.entries.clear()
    variant_cache.set(image_id, "jpeg-qdefault", b"cached jpeg")

    with render_slots.hold():
        response = client.get(rendered)

        assert response.status_code == 503
        assert response.json() == {"detail": "Too many conversions in progress; retry shortly"}
        assert response.headers["retry-after"] == "5"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == "*"
        # Only renders need a slot.
        assert client.get(signed_url(key, image["id"], "png")).status_code == 200
        assert client.get(signed_url(key, image["id"], "jpg")).content == b"cached jpeg"
        assert client.get(rendered, headers={"If-None-Match": etag}).status_code == 304
        # Share links' conversions have slots of their own.
        link = share(client, auth_headers, image["id"], format="webp")
        assert client.get(link["url"]).status_code == 200

    assert client.get(rendered).status_code == 200


def test_share_links_busy_slots_leave_signed_urls_rendering(client, auth_headers, signer, key):
    image = upload(client, auth_headers)
    share_slots = ConversionSlots(1, max_waiting=0)
    app.dependency_overrides[get_share_conversion_slots] = lambda: share_slots
    link = share(client, auth_headers, image["id"], format="webp")

    with share_slots.hold():
        assert client.get(link["url"]).status_code == 503
        assert client.get(signed_url(key, image["id"], "webp", "w_32")).status_code == 200


def test_one_account_cannot_take_every_render_slot(client, auth_headers, signer, key):
    slots = ConversionSlots(2, max_waiting=4, wait_seconds=0.05, owner_slots=1, owner_waiting=2)
    app.dependency_overrides[get_signed_render_slots] = lambda: slots
    image = upload(client, auth_headers)
    bob = register(client, "bob")
    bob_key = create_url_signing_key(client, bob)
    bob_image = upload(client, bob)

    with slots.hold(user_id_of(auth_headers)):  # one of alice's renders, in progress
        assert client.get(signed_url(key, image["id"], "webp", "w_32")).status_code == 503
        assert client.get(signed_url(bob_key, bob_image["id"], "webp", "w_32")).status_code == 200

    assert client.get(signed_url(key, image["id"], "webp", "w_32")).status_code == 200


def test_a_render_that_waited_uses_what_was_cached_meanwhile(
    client, auth_headers, signer, key, storage, variant_cache, render_rate_limiter
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", "w_32")
    image_id = uuid.UUID(image["id"])
    parsed = parse_options("w_32", "webp")
    token = transformation_variant("png", parsed.spec, parsed.transformations).token

    class FinishesAnotherRenderFirst(ConversionSlots):
        def hold(self, *args):
            # Another request rendered the same variant while this one waited.
            variant_cache.set(image_id, token, b"rendered meanwhile")
            return super().hold(*args)

    app.dependency_overrides[get_signed_render_slots] = lambda: FinishesAnotherRenderFirst(1)
    no_storage(storage)

    assert client.get(url).content == b"rendered meanwhile"
    assert render_rate_limiter.hits == []  # nothing rendered, nothing charged


# --- Render budget --------------------------------------------------------------------------------


def test_renders_cost_their_account_the_megapixels_they_make(
    client, auth_headers, signer, key, variant_cache, render_rate_limiter
):
    image = upload(client, auth_headers, make_image_bytes(size=(2000, 1000)))
    owner = user_id_of(auth_headers)
    costs = {"grayscale": 2, "w_3000": math.ceil(3000 * 1500 / 1e6), "w_10": 1, "q_50": 2}

    for options in costs:
        assert client.get(signed_url(key, image["id"], "jpg", options)).status_code == 200

    assert render_rate_limiter.checks == [(owner, cost) for cost in costs.values()]
    assert render_rate_limiter.costs == list(costs.values())
    assert render_rate_limiter.hits == [owner] * len(costs)


def test_renders_of_large_images_cost_a_quarter_of_their_megapixels_at_least(
    client, auth_headers, signer, key, render_rate_limiter
):
    """Decoding a 12-megapixel image isn't free, however little of it a render keeps."""
    image = upload(client, auth_headers, make_image_bytes(size=(4000, 3000)))

    for options in ("crop_0_0_1_1", "w_100", "w_5000"):
        assert client.get(signed_url(key, image["id"], "jpg", options)).status_code == 200

    assert render_rate_limiter.costs == [3, 3, math.ceil(5000 * 3750 / 1e6)]


def test_a_variant_too_large_to_cache_is_rendered_each_time_but_charged_once(
    client, auth_headers, signer, key, storage, variant_cache, render_rate_limiter
):
    variant_cache.max_item_bytes = 100
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "png", "w_32")
    reads = []
    read = storage.read
    storage.read = lambda key: reads.append(key) or read(key)

    first = client.get(url)
    render_rate_limiter.decision = REJECTED  # the account's budget is used up now
    again = [client.get(url) for _ in range(3)]

    assert len(first.content) > 100
    for response in again:
        assert response.status_code == 200
        assert response.content == first.content
    assert len(reads) == 4  # rendered each time...
    assert render_rate_limiter.hits == [user_id_of(auth_headers)]  # ...but charged once
    assert len(render_rate_limiter.checks) == 1
    # Another variant is charged as usual (and refused now).
    assert client.get(signed_url(key, image["id"], "png", "w_33")).status_code == 429
    # Deleting the image forgets it.
    client.delete(f"/images/{image['id']}", headers=auth_headers)
    assert uuid.UUID(image["id"]) not in variant_cache.entries


def test_a_storage_outage_costs_the_account_nothing(
    client, auth_headers, signer, key, storage, render_rate_limiter
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", "w_32")
    read = storage.read

    def unavailable(key):
        raise StorageUnavailableError("down")

    storage.read = unavailable
    assert [client.get(url).status_code for _ in range(3)] == [503] * 3
    assert render_rate_limiter.costs == []

    storage.read = read
    assert client.get(url).status_code == 200
    assert render_rate_limiter.costs == [1]


def test_an_account_over_its_render_budget_gets_429_without_waiting(
    client, auth_headers, signer, key, variant_cache, render_rate_limiter
):
    image = upload(client, auth_headers)
    image_id = uuid.UUID(image["id"])
    variant_cache.set(image_id, "jpeg-qdefault", b"cached jpeg")
    render_rate_limiter.decision = REJECTED
    app.dependency_overrides[get_signed_render_slots] = lambda: NoSlots(1)

    response = client.get(signed_url(key, image["id"], "webp", "w_32"))

    assert response.status_code == 429
    assert response.json() == {
        "detail": "Too many renders through this account's signed URLs; retry in 13s"
    }
    # Viewers never learn the account's usage: no RateLimit fields.
    assert without_date(response) == {
        "retry-after": "13",
        "cache-control": "no-store",
        "access-control-allow-origin": "*",
        "content-type": "application/json",
        "content-length": str(len(response.content)),
    }
    assert render_rate_limiter.checks == [(user_id_of(auth_headers), 1)]
    assert render_rate_limiter.hits == []  # checked, not counted
    # What needs no render is still served.
    assert client.get(signed_url(key, image["id"], "png")).status_code == 200
    assert client.get(signed_url(key, image["id"], "jpg")).content == b"cached jpeg"


def test_a_render_refused_when_charged_frees_its_slot(
    client, auth_headers, signer, key, render_slots, render_rate_limiter
):
    image = upload(client, auth_headers)

    class RefusesWhenCounting(FakeRateLimiter):
        def hit(self, user_id, cost=1):
            super().hit(user_id, cost)
            return REJECTED

    limiter = RefusesWhenCounting()
    app.dependency_overrides[get_render_rate_limiter] = lambda: limiter

    response = client.get(signed_url(key, image["id"], "webp", "w_32"))

    assert response.status_code == 429
    assert len(limiter.checks) == len(limiter.hits) == 1
    assert render_slots._held == 0
    app.dependency_overrides[get_render_rate_limiter] = lambda: render_rate_limiter
    assert client.get(signed_url(key, image["id"], "webp", "w_32")).status_code == 200


def test_the_render_budget_is_counted_in_redis(client, auth_headers, signer, key, redis_client):
    owner = user_id_of(auth_headers)
    counter = f"{RENDER_KEY_PREFIX}:{owner.hex}:minute"
    limiter = RateLimiter(redis_client, (Limit("minute", 3, 60),), key_prefix=RENDER_KEY_PREFIX)
    app.dependency_overrides[get_render_rate_limiter] = lambda: limiter
    image = upload(client, auth_headers, make_image_bytes(size=(2000, 1000)))
    try:
        first = client.get(signed_url(key, image["id"], "jpg", "grayscale"))  # 2 megapixels
        second = client.get(signed_url(key, image["id"], "jpg", "sepia"))  # 2 more: over 3
        counted = redis_client.get(counter)
        third = client.get(signed_url(key, image["id"], "jpg", "w_1000"))  # 0.5, as 1: fits

        assert first.status_code == 200
        assert second.status_code == 429
        assert 1 <= int(second.headers["retry-after"]) <= 60
        assert counted == b"2"
        assert third.status_code == 200
        assert redis_client.get(counter) == b"3"
    finally:
        redis_client.unlink(counter)


def test_signed_urls_render_while_redis_is_down(
    client, auth_headers, signer, key, closed_port, caplog
):
    down = create_redis_client(f"redis://127.0.0.1:{closed_port}/0", 0.25)
    cache = VariantCache(down, ttl_seconds=60, max_item_bytes=1024**2)
    limiter = build_render_rate_limiter(get_settings(), down)
    app.dependency_overrides[get_variant_cache] = lambda: cache
    app.dependency_overrides[get_render_rate_limiter] = lambda: limiter
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "webp", "w_32")

    with caplog.at_level(logging.WARNING):
        responses = [client.get(url) for _ in range(2)]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    messages = [record.getMessage() for record in caplog.records]
    assert any("signed URL renders are not rate limited" in message for message in messages)


# --- Failures -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("options", "method"), [(None, "open"), ("w_32", "read")])
def test_a_storage_outage_is_503_and_the_log_hides_the_signature(
    client, auth_headers, signer, key, storage, caplog, options, method
):
    image = upload(client, auth_headers)
    url = signed_url(key, image["id"], "png", options)

    def unavailable(*args, **kwargs):
        raise StorageUnavailableError("down")

    setattr(storage, method, unavailable)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = client.get(url)

    assert response.status_code == 503
    assert response.json() == {"detail": "Image storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "*"
    [record] = [record for record in caplog.records if record.name == "app.main"]
    signature = url.split("/signed/", 1)[1].split("/")[1]
    assert f"/signed/{key['key_id']}/[redacted]/" in record.getMessage()
    assert signature not in record.getMessage()


def test_head_is_not_supported(client, auth_headers, signer, key):
    image = upload(client, auth_headers)

    assert client.head(signed_url(key, image["id"], "png")).status_code == 405


def test_openapi_documents_signed_urls():
    paths = app.openapi()["paths"]
    download = paths["/signed/{key_id}/{signature}/{signed_path}"]["get"]

    assert "security" not in download
    assert {"200", "304", "404", "410", "422", "429", "503"} <= set(download["responses"])
    assert [path for path in paths if path.startswith("/signed/")] == [
        "/signed/{key_id}/{signature}/{signed_path}"
    ]
