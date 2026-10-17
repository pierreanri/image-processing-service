"""Signing and checking signed URLs (app/signed_urls.py). No database."""

import base64
import hmac
import re
import uuid

import pytest

from app.sharing import ShareSigner
from app.signed_urls import KEY_PREFIX, SignedPath, UrlSigner, sign
from app.webhooks import WebhookSecrets

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
