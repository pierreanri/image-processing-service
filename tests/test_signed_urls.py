"""Signing, checking and reading signed URLs (app/signed_urls.py), and predicting render sizes.
No database."""

import base64
import hmac
import logging
import re
import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.imaging import TransformationError, apply_transformations, load_image, predict_output_size
from app.routers.downloads import transformation_variant
from app.schemas import TransformationSpec
from app.sharing import RedactUrlCredentials, ShareSigner, redact_url_credentials
from app.signed_urls import (
    KEY_PREFIX,
    InvalidOptions,
    SignedOptions,
    SignedPath,
    UrlSigner,
    format_options,
    get_signed_render_slots,
    parse_options,
    sign,
)
from app.webhooks import WebhookSecrets
from tests.utils import make_image_bytes

SECRET = "test-secret-that-is-at-least-32-characters-long"
KEY_ID = "AAECAwQFBgcICQoL"
# Pinned: changing the derivation would silently invalidate every account's key.
KEY = "urlsec_Hp2rJEW6nATQNkMfVNlPS7aq8nm4jf0N-6LureE54gw"
IMAGE_ID = "bd06a108-6cf7-46fb-aea5-9c4437d80144"
OPTIONS = "w_800,h_600,c_cover,wm_wqkgQWxpY2UsIGEvYl9j,g_top-left,o_30,q_80,exp_1791204110"
VECTORS = [
    (f"{IMAGE_ID}.jpg", "TLRzPAzW0FViIR_fgbALN240"),
    (f"{OPTIONS}/{IMAGE_ID}.webp", "PXOraXUEvaKLf6kVuLzhWcxS"),
]


def test_keys_are_derived_from_the_secret_and_the_key_id():
    signer = UrlSigner(SECRET)

    assert signer.key(KEY_ID) == KEY
    assert re.fullmatch(r"urlsec_[A-Za-z0-9_-]{43}", KEY)
    assert signer.key(KEY_ID) == UrlSigner(SECRET).key(KEY_ID)
    assert signer.key("BAECAwQFBgcICQoL") != KEY
    assert UrlSigner("another-secret-that-is-at-least-32-chars").key(KEY_ID) != KEY


def test_url_keys_have_a_derivation_of_their_own():
    """Nothing else the service signs or derives can stand for a URL key, nor the other way
    round."""
    signer = UrlSigner(SECRET)
    raw = base64.urlsafe_b64decode(signer.key(KEY_ID).removeprefix(KEY_PREFIX) + "=")

    assert raw != hmac.digest(SECRET.encode(), KEY_ID.encode(), "sha256")
    assert raw != ShareSigner(SECRET)._key
    assert raw != WebhookSecrets(SECRET).key(uuid.UUID(int=0), KEY_ID.encode()[:16])
    assert SECRET not in signer.key(KEY_ID)


def test_key_ids_are_random_and_url_safe():
    ids = {UrlSigner.new_key_id() for _ in range(100)}

    assert len(ids) == 100
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{16}", key_id) for key_id in ids)


@pytest.mark.parametrize(("signed_path", "signature"), VECTORS)
def test_signatures_match_the_vectors(signed_path, signature):
    assert sign(KEY, signed_path) == signature


def test_a_signed_path_verifies():
    signer = UrlSigner(SECRET)

    assert signer.verify(KEY_ID, *reversed(VECTORS[0])) == SignedPath(
        None, uuid.UUID(IMAGE_ID), "jpeg"
    )
    assert signer.verify(KEY_ID, VECTORS[1][1], VECTORS[1][0]) == SignedPath(
        OPTIONS, uuid.UUID(IMAGE_ID), "webp"
    )


def _changes(text):
    """`text` with each of its characters changed in turn (to another URL-safe character)."""
    for index, char in enumerate(text):
        yield text[:index] + ("A" if char != "A" else "B") + text[index + 1 :]


def test_any_change_breaks_the_signature():
    signer = UrlSigner(SECRET)
    signed_path, signature = VECTORS[1]

    for key_id in _changes(KEY_ID):
        assert signer.verify(key_id, signature, signed_path) is None
    for changed in _changes(signature):
        assert signer.verify(KEY_ID, changed, signed_path) is None
    for changed in _changes(signed_path):
        assert signer.verify(KEY_ID, signature, changed) is None
    assert (
        UrlSigner("another-secret-that-is-at-least-32-chars").verify(KEY_ID, signature, signed_path)
        is None
    )


@pytest.mark.parametrize(
    ("key_id", "signature", "signed_path"),
    [
        (KEY_ID[:-1], VECTORS[0][1], VECTORS[0][0]),
        (KEY_ID + "A", VECTORS[0][1], VECTORS[0][0]),
        (KEY_ID[:-1] + "=", VECTORS[0][1], VECTORS[0][0]),
        (KEY_ID[:-1] + "+", VECTORS[0][1], VECTORS[0][0]),
        (KEY_ID, VECTORS[0][1][:-1], VECTORS[0][0]),
        (KEY_ID, VECTORS[0][1] + "A", VECTORS[0][0]),
        (KEY_ID, VECTORS[0][1][:-1] + "/", VECTORS[0][0]),
        (KEY_ID, VECTORS[0][1][:-1] + "!", VECTORS[0][0]),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID}.jpeg"),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID}.JPG"),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID}.tif"),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID.upper()}.jpg"),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID.replace('-', '')}.jpg"),
        (KEY_ID, VECTORS[0][1], f"/{IMAGE_ID}.jpg"),
        (KEY_ID, VECTORS[0][1], f"{IMAGE_ID}.jpg/"),
        (KEY_ID, VECTORS[0][1], f"w_8//{IMAGE_ID}.jpg"),
        (KEY_ID, VECTORS[0][1], f"{'w' * 1025}/{IMAGE_ID}.jpg"),
        (KEY_ID, VECTORS[0][1], f"w_8%2C/{IMAGE_ID}.jpg"),
        (KEY_ID, VECTORS[0][1], f"a/b/{IMAGE_ID}.jpg"),
    ],
)
def test_malformed_urls_are_refused_before_any_hmac(key_id, signature, signed_path, monkeypatch):
    monkeypatch.setattr("app.signed_urls.sign", lambda *args: pytest.fail("signed"))

    assert UrlSigner(SECRET).verify(key_id, signature, signed_path) is None


def test_options_may_be_as_long_as_the_limit():
    signer = UrlSigner(SECRET)
    signed_path = f"{'w' * 1024}/{IMAGE_ID}.png"

    assert signer.verify(KEY_ID, sign(KEY, signed_path), signed_path) is not None


# --- Options --------------------------------------------------------------------------------------

CANONICAL = [
    ("crop_0_0_400_300", "png"),
    ("crop_12_7_40_30", "png"),
    ("w_800", "png"),
    ("h_600", "png"),
    ("w_800,h_600", "png"),
    ("w_800,h_600,c_contain", "png"),
    ("w_800,h_600,c_cover", "png"),
    ("a_90", "png"),
    ("a_270", "png"),
    ("a_45.5", "png"),
    ("a_0.01", "png"),
    ("a_359.99", "png"),
    ("flip", "png"),
    ("mirror", "png"),
    ("flip,mirror", "png"),
    ("blur_2.5", "png"),
    ("blur_50", "png"),
    ("sharpen", "png"),
    ("grayscale", "png"),
    ("sepia", "png"),
    ("blur_0.25,sharpen,grayscale,sepia", "png"),
    ("wm_SGk", "png"),
    ("wm_SGk,g_top-left", "png"),
    ("wm_SGk,g_top-right,o_0", "png"),
    ("wm_SGk,g_bottom-left,o_1,fs_8", "png"),
    ("wm_SGk,g_center,o_100,fs_500", "png"),
    ("q_80", "jpeg"),
    ("q_1", "webp"),
    ("exp_1791204110", "png"),
    ("exp_9999999999", "png"),
    ("exp_1", "png"),
    (
        "crop_0_0_400_300,w_200,h_100,c_cover,a_90,flip,mirror,blur_1,sharpen,grayscale,sepia,"
        "wm_wqkgQWxpY2UsIGEvYl9j,g_top-left,o_30,fs_24,q_80,exp_1791204110",
        "webp",
    ),
]


@pytest.mark.parametrize(("options", "image_format"), CANONICAL)
def test_canonical_options_round_trip(options, image_format):
    parsed = parse_options(options, image_format)

    assert format_options(parsed.spec, parsed.expires) == options
    assert parsed.spec.format == image_format
    assert "exp_" not in parsed.transformations
    assert parsed.transformations == format_options(parsed.spec)


def test_the_expiry_is_kept_apart():
    parsed = parse_options("w_8,exp_1791204110", "png")

    assert (parsed.transformations, parsed.expires) == ("w_8", 1791204110)
    assert parse_options(None, "png") == SignedOptions(TransformationSpec(format="png"), "", None)


@pytest.mark.parametrize(
    ("options", "image_format", "canonical"),
    [
        ("h_600,w_800", "png", "w_800,h_600"),
        ("w_800,h_600,c_fill", "png", "w_800,h_600"),
        ("a_-90", "png", "a_270"),
        ("a_90.0", "png", "a_90"),
        ("a_090", "png", "a_90"),
        ("blur_2.50", "png", "blur_2.5"),
        ("w_0800", "png", "w_800"),
        ("crop_00_0_4_3", "png", "crop_0_0_4_3"),
        ("mirror,flip", "png", "flip,mirror"),
        ("sepia,grayscale", "png", "grayscale,sepia"),
        ("wm_SGk,g_bottom-right", "png", "wm_SGk"),
        ("wm_SGk,o_50", "png", "wm_SGk"),
        ("o_30,wm_SGk", "png", "wm_SGk,o_30"),
        ("wm_SGl", "png", "wm_SGk"),
        ("exp_1791204110,w_8", "png", "w_8,exp_1791204110"),
        ("exp_01", "png", "exp_1"),
    ],
)
def test_other_spellings_are_refused_with_the_canonical_one(options, image_format, canonical):
    with pytest.raises(InvalidOptions, match=re.escape(f'sign "{canonical}" instead')):
        parse_options(options, image_format)


@pytest.mark.parametrize(
    ("options", "image_format"),
    [
        ("a_0", "png"),
        ("a_360", "png"),
        ("q_80", "png"),
        ("q_80", "gif"),
        ("q_80", "bmp"),
        ("q_80", "tiff"),
    ],
)
def test_options_that_change_nothing_are_refused(options, image_format):
    with pytest.raises(InvalidOptions, match="leave the options segment out"):
        parse_options(options, image_format)


SENTINEL = "987654321"


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ("f_webp", "the format is the file extension"),
        ("e_sepia", "unknown option e_"),
        ("bogus", "unknown option bogus"),
        ("FLIP", "unknown option FLIP"),
        ("flip_1", "unknown option flip_"),
        ("flip,flip", "flip is given twice"),
        ("w_8,w_9", "w is given twice"),
        ("w_8,,flip", "empty option"),
        ("w_8,", "empty option"),
        (",w_8", "empty option"),
        ("g_center", "g_ is only allowed with wm_"),
        ("o_30", "o_ is only allowed with wm_"),
        ("fs_20", "fs_ is only allowed with wm_"),
        ("c_cover", "c_ is only allowed with both w_ and h_"),
        ("w_8,c_cover", "c_ is only allowed with both w_ and h_"),
        ("w_8,h_9,c_stretch", "c_: expected c_contain or c_cover"),
        ("wm_SGk,g_middle", "g_: expected one of"),
        ("crop_1_2_3", "crop_: expected crop_X_Y_WIDTH_HEIGHT"),
        ("w_-8", "w_: expected a whole number"),
        ("w_1.5", "w_: expected a whole number"),
        (f"w_{SENTINEL}0", "w_: expected a whole number"),
        ("a_1e2", "a_: expected a number"),
        ("blur_.5", "blur_: expected a number"),
        ("wm_", "wm_: expected the text's UTF-8 bytes"),
        ("wm_S", "wm_: expected the text's UTF-8 bytes"),
        ("wm__w", "wm_: expected the text's UTF-8 bytes"),
        ("wm_gA", "wm_: expected the text's UTF-8 bytes"),
        (f"q_{SENTINEL}", "q_: Input should be less than or equal to 100"),
        (f"wm_SGk,fs_{SENTINEL}", "fs_: Input should be less than or equal to 500"),
        ("w_0", "w_: Input should be greater than 0"),
        ("h_0", "h_: Input should be greater than 0"),
        ("crop_0_0_0_3", "crop_: Input should be greater than 0"),
        ("a_400", "a_: Input should be less than or equal to 360"),
        ("blur_0", "blur_: Input should be greater than 0"),
        ("blur_50.01", "blur_: Input should be less than or equal to 50"),
        ("wm_SGk,fs_7", "fs_: Input should be greater than or equal to 8"),
        ("wm_SGk,fs_501", "fs_: Input should be less than or equal to 500"),
        ("wm_SGk,o_101", "o_: expected a percentage from 0 to 100"),
        ("q_0", "q_: Input should be greater than or equal to 1"),
        ("q_101", "q_: Input should be less than or equal to 100"),
        ("exp_0", "exp_: must be Unix seconds"),
        ("exp_10000000000", "exp_: must be Unix seconds"),
        ("exp", "unknown option exp"),
    ],
)
def test_invalid_options_say_why_without_repeating_values(options, reason):
    with pytest.raises(InvalidOptions) as caught:
        parse_options(options, "jpeg")

    assert reason in str(caught.value)
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "©",
        "\U0001f600" * 100,
        "שלום עולם",
        ",/_=%+?#",
        "a b",
        "two\nlines",
        "x" * 100,
    ],
)
def test_watermark_text_round_trips(text):
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    parsed = parse_options(f"wm_{encoded}", "png")

    assert parsed.spec.watermark.text == text
    assert format_options(parsed.spec) == f"wm_{encoded}"


def test_watermark_text_is_at_most_100_characters():
    encoded = base64.urlsafe_b64encode(("x" * 101).encode()).decode().rstrip("=")

    with pytest.raises(InvalidOptions, match="wm_: String should have at most 100 characters"):
        parse_options(f"wm_{encoded}", "png")


def test_every_two_decimal_angle_and_radius_has_one_spelling():
    for hundredths in range(1, 36000):
        value = hundredths / 100
        spelled = f"{value:.2f}".rstrip("0").rstrip(".")
        assert format_options(parse_options(f"a_{spelled}", "png").spec) == f"a_{spelled}"
        if value <= 50:
            assert format_options(parse_options(f"blur_{spelled}", "png").spec) == (
                f"blur_{spelled}"
            )


# --- Variants -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "stored", "extension_format", "token"),
    [
        (None, "png", "png", None),
        (None, "png", "webp", "webp-qdefault"),
        ("q_70", "jpeg", "jpeg", "jpeg-q70"),
        ("q_70", "png", "jpeg", "jpeg-q70"),
        (None, "jpeg", "jpeg", None),
    ],
)
def test_conversions_share_the_tokens_of_downloads(options, stored, extension_format, token):
    parsed = parse_options(options, extension_format)

    assert transformation_variant(stored, parsed.spec, parsed.transformations).token == token


def test_transformations_get_a_token_of_their_own():
    def token(options, image_format="webp"):
        parsed = parse_options(options, image_format)
        return transformation_variant("png", parsed.spec, parsed.transformations).token

    assert re.fullmatch(r"webp-t[0-9a-f]{32}", token("w_8"))
    assert token("w_8") == token("w_8,exp_1791204110") == token("w_8,exp_1791204111")
    assert token("w_8") != token("w_9") != token("w_8,flip")
    assert token("w_8", "png") == "png" + token("w_8").removeprefix("webp")
    assert token("w_8,q_80") != token("w_8")


# --- Predicting sizes -----------------------------------------------------------------------------


GEOMETRIES = [
    {},
    {"crop": {"x": 10, "y": 5, "width": 40, "height": 30}},
    {"crop": {"x": 100, "y": 0, "width": 40, "height": 30}},
    {"resize": {"width": 33}},
    {"resize": {"height": 17}},
    {"resize": {"width": 30, "height": 50, "fit": "fill"}},
    {"resize": {"width": 30, "height": 50, "fit": "contain"}},
    {"resize": {"width": 300, "height": 5, "fit": "contain"}},
    {"resize": {"width": 30, "height": 50, "fit": "cover"}},
    {"resize": {"width": 3000}},
    {"rotate": 90},
    {"rotate": -90},
    {"rotate": 45.5},
    {"rotate": 359.99},
    {"resize": {"width": 900, "height": 900}, "rotate": 45},
    {"crop": {"x": 0, "y": 0, "width": 7, "height": 3}, "resize": {"width": 1}, "rotate": 30},
    {"flip": True, "mirror": True, "filters": {"blur": 3}},
]


@pytest.mark.parametrize("geometry", GEOMETRIES)
@pytest.mark.parametrize("image_format", ["png", "webp"])
def test_predicted_sizes_are_what_renders_make(geometry, image_format):
    loaded = load_image(make_image_bytes(size=(120, 80)), 10**8)
    spec = TransformationSpec.model_validate({**geometry, "format": image_format})

    def outcome(run):
        try:
            return run()
        except TransformationError as exc:
            return str(exc)

    rendered = outcome(lambda: _size(apply_transformations(loaded, spec, 1000)))
    predicted = outcome(lambda: predict_output_size(120, 80, spec, image_format, 1000))

    assert predicted == rendered


def _size(encoded):
    return encoded.width, encoded.height


def test_rotations_too_large_are_refused_before_rotating(monkeypatch):
    """The expanded canvas would be built only to be refused."""
    loaded = load_image(make_image_bytes(size=(1, 1)), 10**8)
    spec = TransformationSpec(resize={"width": 900, "height": 900}, rotate=45, format="png")
    monkeypatch.setattr(
        "app.imaging._rotate", lambda *args: pytest.fail("the canvas was allocated")
    )

    with pytest.raises(TransformationError, match="Resulting image 1274x1274 exceeds"):
        apply_transformations(loaded, spec, 1000)


# --- Render slots and settings --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slots", "waiting", "owner_slots"), [(1, 4, 1), (2, 8, 1), (4, 8, 2), (8, 8, 4)]
)
def test_render_slots_follow_the_setting(monkeypatch, slots, waiting, owner_slots):
    settings = Settings(
        _env_file=None, jwt_secret="x" * 32, signed_url_max_concurrent_renders=slots
    )
    monkeypatch.setattr("app.signed_urls.get_settings", lambda: settings)
    get_signed_render_slots.cache_clear()
    try:
        made = get_signed_render_slots()
    finally:
        get_signed_render_slots.cache_clear()

    assert (made._slots, made._max_waiting, made._owner_slots) == (slots, waiting, owner_slots)
    assert made._owner_waiting == slots


@pytest.mark.parametrize(
    "values",
    [
        {"signed_url_max_concurrent_renders": 0},
        {"signed_url_max_concurrent_renders": 9},
        {"signed_url_render_megapixels_per_minute": -1},
        {"signed_url_render_megapixels_per_hour": -1},
    ],
)
def test_invalid_signed_url_settings_are_refused(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="x" * 32, **values)


# --- Log redaction --------------------------------------------------------------------------------


def test_signatures_are_masked_in_logs():
    path = f"/signed/{KEY_ID}/{VECTORS[1][1]}/{VECTORS[1][0]}"

    redacted = redact_url_credentials(f"GET {path} HTTP/1.1")

    assert VECTORS[1][1] not in redacted
    assert f"/signed/{KEY_ID}/[redacted]/{OPTIONS}/{IMAGE_ID}.webp" in redacted


def test_uvicorns_access_lines_are_masked_too():
    """uvicorn logs the path re-quoted (commas become %2C)."""
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:5000",
            "GET",
            f"/signed/{KEY_ID}/{VECTORS[1][1]}/w_800%2Ch_600/{IMAGE_ID}.webp",
            "1.1",
            200,
        ),
        None,
    )

    RedactUrlCredentials().filter(record)

    assert VECTORS[1][1] not in record.getMessage()
    assert f"/signed/{KEY_ID}/[redacted]/w_800%2Ch_600/" in record.getMessage()
