"""Share links: signed URLs that download one image, in one variant, without a bearer token.

Nothing is stored per link. A link's token is 60 base64url characters: a 27-byte payload

    version (1 byte) | image id (16) | share generation (4) | expiry, Unix seconds (4)
    | format code (1) | quality (1; 0 for the format's default)

followed by the first 18 bytes of its HMAC-SHA256, under a key derived from JWT_SECRET. 45 bytes
are exactly 60 base64 characters, so a link has only one spelling, and its first 36 characters
are the payload. A link works until it expires, its image is deleted, or its image's share
generation changes: revoking an image's links increments it, so links issued before that carry
an older value. The token is signed, not encrypted: its holder can read what the payload says.
"""

import base64
import hmac
import logging
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings

VERSION = 1
# How long a link lasts when its creator doesn't say (capped by SHARE_MAX_TTL_SECONDS).
DEFAULT_LIFETIME_SECONDS = 24 * 60 * 60
# Append-only: the position of a format is inside links already handed out.
FORMAT_CODES = ("jpeg", "png", "webp", "gif", "bmp", "tiff")
# How long a download through a link waits for a conversion slot before giving up with 503.
CONVERSION_WAIT_SECONDS = 5.0

_PAYLOAD = struct.Struct(">B16sIIBB")
_TAG_BYTES = 18
_TOKEN = re.compile(r"[A-Za-z0-9_-]{60}")
# Gives share links a key of their own: a JWT signature can never pass for a link's tag, nor
# the other way round.
_KEY_LABEL = b"imgsvc share links v1"


@dataclass(frozen=True)
class ShareLink:
    image_id: uuid.UUID
    generation: int
    # Unix seconds: the link works while the time is before this.
    expires: int
    # A key of app.imaging.FORMATS.
    format: str
    # None: the format's default.
    quality: int | None


class ShareSigner:
    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        self._key = hmac.digest(secret.encode(), _KEY_LABEL, "sha256")
        self._clock = clock

    def now(self) -> float:
        """The current time in Unix seconds, as link expiry sees it."""
        return self._clock()

    def sign(self, link: ShareLink) -> str:
        payload = _PAYLOAD.pack(
            VERSION,
            link.image_id.bytes,
            link.generation,
            link.expires,
            FORMAT_CODES.index(link.format),
            link.quality or 0,
        )
        return base64.urlsafe_b64encode(payload + self._tag(payload)).decode()

    def verify(self, token: str) -> ShareLink | None:
        """The link `token` stands for, or None if this service didn't issue it. Its expiry isn't
        checked here."""
        # The decoder silently skips characters outside the alphabet, so check the shape first.
        if not _TOKEN.fullmatch(token):
            return None
        raw = base64.urlsafe_b64decode(token)
        payload, tag = raw[: _PAYLOAD.size], raw[_PAYLOAD.size :]
        if not hmac.compare_digest(self._tag(payload), tag):
            return None
        version, image_id, generation, expires, code, quality = _PAYLOAD.unpack(payload)
        # Only a later version of this code could have signed these.
        if version != VERSION or code >= len(FORMAT_CODES) or quality > 100:
            return None
        return ShareLink(
            uuid.UUID(bytes=image_id), generation, expires, FORMAT_CODES[code], quality or None
        )

    def _tag(self, payload: bytes) -> bytes:
        return hmac.digest(self._key, payload, "sha256")[:_TAG_BYTES]


@lru_cache
def get_share_signer() -> ShareSigner:
    return ShareSigner(get_settings().jwt_secret)


class ConversionsBusyError(Exception):
    """Every conversion slot is taken and enough requests already wait for one. Answered with
    503."""


class ConversionSlots:
    """Bounds the conversions that downloads through share links run at once in this process.
    Anyone holding a link can make the API convert its image whenever that variant isn't cached
    (Redis down, or larger than CACHE_MAX_ITEM_BYTES), and an embedded link has many viewers.

    A request without a free slot waits up to `wait_seconds` for one, but only `max_waiting` may
    wait at a time (each holds one of the API's worker threads); the others fail at once.
    """

    def __init__(
        self,
        slots: int,
        *,
        max_waiting: int | None = None,
        wait_seconds: float = CONVERSION_WAIT_SECONDS,
    ) -> None:
        self._slots = threading.BoundedSemaphore(slots)
        self._max_waiting = slots if max_waiting is None else max_waiting
        self._wait_seconds = wait_seconds
        self._waiting = 0
        self._lock = threading.Lock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold a slot for the duration of the block. Raises ConversionsBusyError if none frees
        up in time."""
        if not self._slots.acquire(blocking=False):
            with self._lock:
                if self._waiting >= self._max_waiting:
                    raise ConversionsBusyError
                self._waiting += 1
            try:
                acquired = self._slots.acquire(timeout=self._wait_seconds)
            finally:
                with self._lock:
                    self._waiting -= 1
            if not acquired:
                raise ConversionsBusyError
        try:
            yield
        finally:
            self._slots.release()


@lru_cache
def get_share_conversion_slots() -> ConversionSlots:
    return ConversionSlots(get_settings().share_max_concurrent_conversions)


# A share link in a URL path: its payload (kept, it helps debugging) and the rest (masked).
_SHARE_PATH = re.compile(r"(/shared/[A-Za-z0-9_-]{36})[A-Za-z0-9_-]+")


def redact_share_tokens(text: str) -> str:
    """`text` with the secret part of any share link in it masked."""
    return _SHARE_PATH.sub(r"\1[redacted]", text)


class RedactShareTokens(logging.Filter):
    """Masks share links in log records, such as uvicorn's access log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_share_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_share_tokens(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_share_tokens(arg) if isinstance(arg, str) else arg
                for key, arg in record.args.items()
            }
        return True


# Installed on uvicorn's access logger at startup (app/main.py).
ACCESS_LOG_FILTER = RedactShareTokens()
