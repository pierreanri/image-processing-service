"""Signed URLs: transformations carried in a URL that an account's backend signs itself, with
the account's URL signing key, without calling the API (like Cloudinary's signed delivery URLs):

    /signed/{key id}/{signature}/[{options}/]{image id}.{extension}

The key is never stored: it is derived from JWT_SECRET and the key id, a random public id that
the account's row holds (POST /me/url-signing-key) and every URL carries. So a URL's signature is
checked without any I/O, before anything else, and a forged URL costs two HMACs. The signature
is the first 24 base64url characters of the HMAC-SHA256, keyed with the key string as is, of
everything after "/{signature}/". Rotating the key gives the account a new key id, which ends
every URL signed with the old one.
"""

import base64
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings
from app.imaging import FORMATS

KEY_PREFIX = "urlsec_"
KEY_ID_LENGTH = 16
SIGNATURE_LENGTH = 24
MAX_OPTIONS_LENGTH = 1024
# The extension of a signed URL is the format of what it serves.
EXTENSIONS = {info.extension: name for name, info in FORMATS.items()}

# Gives URL signing keys a derivation of their own, apart from JWTs, share links and webhooks.
_KEY_LABEL = b"imgsvc url signing keys v1"
_KEY_ID = re.compile(rf"[A-Za-z0-9_-]{{{KEY_ID_LENGTH}}}")
_SIGNATURE = re.compile(rf"[A-Za-z0-9_-]{{{SIGNATURE_LENGTH}}}")
# The options segment (if any), the image id exactly as the API writes it, and the extension.
_SIGNED_PATH = re.compile(
    rf"(?:([A-Za-z0-9_.,-]{{1,{MAX_OPTIONS_LENGTH}}})/)?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    rf"\.({'|'.join(EXTENSIONS)})"
)


@dataclass(frozen=True)
class SignedPath:
    """What a signed URL's signature covers."""

    # The options segment as signed; None if the URL has none.
    options: str | None
    image_id: uuid.UUID
    # A key of app.imaging.FORMATS.
    format: str


def sign(key: str, signed_path: str) -> str:
    """The signature of `signed_path` (what follows the signature in the URL) under `key`."""
    digest = hmac.digest(key.encode(), signed_path.encode(), "sha256")
    return base64.urlsafe_b64encode(digest).decode()[:SIGNATURE_LENGTH]


class UrlSigner:
    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        self._key = hmac.digest(secret.encode(), _KEY_LABEL, "sha256")
        self._clock = clock

    def now(self) -> float:
        """The current time in Unix seconds, as URL expiry sees it."""
        return self._clock()

    @staticmethod
    def new_key_id() -> str:
        return secrets.token_urlsafe(12)

    def key(self, key_id: str) -> str:
        """The URL signing key with this id, as the account is given it."""
        digest = hmac.digest(self._key, key_id.encode(), "sha256")
        return KEY_PREFIX + base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def verify(self, key_id: str, signature: str, signed_path: str) -> SignedPath | None:
        """What a URL's signed path says, or None if it wasn't signed with the key `key_id`
        names. Whether that key is still an account's current one isn't checked here."""
        # Shapes first: nothing else costs anything, and the HMAC then only sees ASCII.
        if not (_KEY_ID.fullmatch(key_id) and _SIGNATURE.fullmatch(signature)):
            return None
        match = _SIGNED_PATH.fullmatch(signed_path)
        if match is None:
            return None
        if not hmac.compare_digest(sign(self.key(key_id), signed_path), signature):
            return None
        options, image_id, extension = match.groups()
        return SignedPath(options, uuid.UUID(image_id), EXTENSIONS[extension])


@lru_cache
def get_url_signer() -> UrlSigner:
    return UrlSigner(get_settings().jwt_secret)
