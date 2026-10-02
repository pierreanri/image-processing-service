import base64
import hmac
import logging
import string
import threading
import time
import uuid

import pytest
from pydantic import ValidationError
from uvicorn.logging import AccessFormatter

from app.config import Settings
from app.imaging import FORMATS
from app.sharing import (
    _PAYLOAD,
    ACCESS_LOG_FILTER,
    FORMAT_CODES,
    ConversionsBusyError,
    ConversionSlots,
    RedactShareTokens,
    ShareLink,
    ShareSigner,
    redact_share_tokens,
)

SECRET = "test-secret-that-is-at-least-32-characters-long"
ALPHABET = string.ascii_letters + string.digits + "-_"


@pytest.fixture
def signer() -> ShareSigner:
    return ShareSigner(SECRET)


def a_link(**changes) -> ShareLink:
    values = {
        "image_id": uuid.UUID("bd06a108-6cf7-46fb-aea5-9c4437d80144"),
        "generation": 3,
        "expires": 1_790_000_000,
        "format": "webp",
        "quality": 70,
    }
    return ShareLink(**(values | changes))


# --- Tokens ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("format", FORMAT_CODES)
@pytest.mark.parametrize("quality", [None, 1, 100])
@pytest.mark.parametrize(
    ("generation", "expires"), [(0, 1), (2**31 - 1, 1_790_000_000), (7, 2**32 - 1)]
)
def test_tokens_round_trip_every_field(signer, format, quality, generation, expires):
    link = a_link(format=format, quality=quality, generation=generation, expires=expires)

    token = signer.sign(link)

    assert len(token) == 60
    assert set(token) <= set(ALPHABET)
    assert signer.verify(token) == link
    # The payload is readable (that's what log redaction keeps), and only the tag is secret.
    payload = _PAYLOAD.pack(1, link.image_id.bytes, generation, expires, 0, 0)
    assert base64.urlsafe_b64decode(token[:36])[1:21] == payload[1:21]


def test_format_codes_never_change():
    # Codes are inside links already handed out: new formats may only be appended.
    assert FORMAT_CODES == ("jpeg", "png", "webp", "gif", "bmp", "tiff")
    assert set(FORMAT_CODES) == set(FORMATS)


def test_signing_is_deterministic(signer):
    assert signer.sign(a_link()) == signer.sign(a_link())
    assert signer.sign(a_link()) != signer.sign(a_link(expires=1_790_000_001))


def test_any_changed_character_is_rejected(signer):
    token = signer.sign(a_link())

    for position, original in enumerate(token):
        for replacement in ALPHABET.replace(original, ""):
            forged = token[:position] + replacement + token[position + 1 :]
            assert signer.verify(forged) is None, (position, replacement)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: "",
        lambda token: token[:-1],
        lambda token: token + "A",
        lambda token: token[:-1] + "=",
        lambda token: token[:59] + "==",
        lambda token: token.replace(token[10], "+", 1),
        lambda token: token.replace(token[10], "/", 1),
        lambda token: token[:30] + "!" + token[31:],
        lambda token: token[:30] + " " + token[31:],
        lambda token: token[:30] + "é" + token[31:],
        lambda token: token + "\n",
        lambda token: token[:30] + token[31:] + "%",
    ],
    ids=[
        "empty",
        "short",
        "long",
        "padding",
        "extra-padding",
        "plus",
        "slash",
        "bang",
        "space",
        "non-ascii",
        "newline",
        "percent",
    ],
)
def test_malformed_tokens_are_rejected_without_errors(signer, mutate):
    assert signer.verify(mutate(signer.sign(a_link()))) is None


def test_tokens_from_another_secret_are_rejected(signer):
    other = ShareSigner("another-secret-that-is-at-least-32-characters")

    assert signer.verify(other.sign(a_link())) is None


def test_the_share_key_is_not_the_jwt_secret(signer):
    """A tag made with JWT_SECRET itself (the key JWTs are signed with) is not valid."""
    payload = _PAYLOAD.pack(1, a_link().image_id.bytes, 3, 1_790_000_000, 2, 70)
    tag = hmac.digest(SECRET.encode(), payload, "sha256")[:18]

    assert signer.verify(base64.urlsafe_b64encode(payload + tag).decode()) is None


@pytest.mark.parametrize(
    ("version", "code", "quality"), [(2, 2, 70), (0, 2, 70), (1, 6, 70), (1, 255, 0), (1, 2, 101)]
)
def test_payloads_this_version_never_signs_are_rejected(signer, version, code, quality):
    payload = _PAYLOAD.pack(version, a_link().image_id.bytes, 3, 1_790_000_000, code, quality)
    token = base64.urlsafe_b64encode(payload + signer._tag(payload)).decode()

    assert signer.verify(token) is None


def test_the_clock_is_injectable(clock):
    clock.now = 1234.5

    assert ShareSigner(SECRET, clock=clock).now() == 1234.5


# --- Redacting logs -------------------------------------------------------------------------------


def test_redaction_keeps_the_payload_and_masks_the_tag(signer):
    token = signer.sign(a_link())
    path = f"/shared/{token}.webp?fbclid=abc"

    redacted = redact_share_tokens(f"GET {path} HTTP/1.1")

    assert redacted == f"GET /shared/{token[:36]}[redacted].webp?fbclid=abc HTTP/1.1"
    assert token[36:] not in redacted
    for untouched in ("/images/abc/content", "/shared/short.png", f"/shared/{token[:36]}.png"):
        assert redact_share_tokens(untouched) == untouched


def test_the_access_log_filter_masks_share_links(signer):
    token = signer.sign(a_link())
    # As uvicorn logs a request.
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/shared/{token}.webp", "1.1", 200),
        None,
    )

    assert RedactShareTokens().filter(record) is True
    line = AccessFormatter(use_colors=False).format(record)

    assert f"/shared/{token[:36]}[redacted].webp" in line
    assert token[36:] not in line


def test_the_access_log_filter_is_installed_once_at_startup(client):
    # The client fixture has run the app's startup, possibly not for the first time.
    filters = logging.getLogger("uvicorn.access").filters

    assert filters.count(ACCESS_LOG_FILTER) == 1


# --- Conversion slots -----------------------------------------------------------------------------


def test_conversion_slots_bound_concurrency():
    slots = ConversionSlots(1, max_waiting=0)

    with slots.hold(), pytest.raises(ConversionsBusyError), slots.hold():
        pass
    with slots.hold():  # released again
        pass


def test_a_slot_is_released_when_the_conversion_fails():
    slots = ConversionSlots(1, max_waiting=0)

    with pytest.raises(RuntimeError), slots.hold():
        raise RuntimeError("conversion failed")

    with slots.hold():
        pass


def test_requests_wait_for_a_slot_but_only_so_many():
    slots = ConversionSlots(1, max_waiting=1, wait_seconds=10)
    got_a_slot = threading.Event()

    def wait_for_a_slot():
        with slots.hold():
            got_a_slot.set()

    with slots.hold():
        waiter = threading.Thread(target=wait_for_a_slot)
        waiter.start()
        deadline = time.monotonic() + 5
        while slots._waiting == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert slots._waiting == 1

        started = time.monotonic()
        with pytest.raises(ConversionsBusyError), slots.hold():
            pass
        assert time.monotonic() - started < 1  # turned away at once, not after waiting
        assert not got_a_slot.is_set()

    waiter.join(5)
    assert got_a_slot.is_set()
    assert slots._waiting == 0


def test_a_request_gives_up_after_waiting_for_a_slot():
    slots = ConversionSlots(1, wait_seconds=0.05)

    with slots.hold(), pytest.raises(ConversionsBusyError), slots.hold():
        pass

    assert slots._waiting == 0


# --- Settings -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"share_max_ttl_seconds": 0},
        {"share_max_ttl_seconds": 366 * 24 * 60 * 60},
        {"share_max_concurrent_conversions": 0},
    ],
)
def test_share_settings_are_validated(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret=SECRET, **values)
