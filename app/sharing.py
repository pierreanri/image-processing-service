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
from collections import Counter
from collections.abc import Callable, Hashable, Iterator
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
    """Bounds the conversions (and renders) that public downloads run at once in this process:
    share links' and signed URLs', each with its own slots. Anyone holding such a URL can make
    the API convert its image whenever that variant isn't cached (Redis down, or larger than
    CACHE_MAX_ITEM_BYTES), and an embedded URL has many viewers.

    A request without a free slot waits up to `wait_seconds` for one, but only `max_waiting` may
    wait at a time (each holds one of the API's worker threads); the others fail at once. With
    `owner_slots`, one owner (an account) holds at most that many slots and has at most
    `owner_waiting` requests waiting, so one account's renders can't take every slot from the
    others.
    """

    def __init__(
        self,
        slots: int,
        *,
        max_waiting: int | None = None,
        wait_seconds: float = CONVERSION_WAIT_SECONDS,
        owner_slots: int | None = None,
        owner_waiting: int | None = None,
    ) -> None:
        self._slots = slots
        self._max_waiting = slots if max_waiting is None else max_waiting
        self._wait_seconds = wait_seconds
        self._owner_slots = owner_slots
        self._owner_waiting = slots if owner_waiting is None else owner_waiting
        self._condition = threading.Condition()
        self._held = 0
        self._waiting = 0
        self._held_by: Counter[Hashable] = Counter()
        self._waiting_by: Counter[Hashable] = Counter()

    @contextmanager
    def hold(self, owner: Hashable | None = None) -> Iterator[None]:
        """Hold a slot (one of `owner`'s) for the duration of the block. Raises
        ConversionsBusyError if none frees up in time."""
        owned = owner is not None and self._owner_slots is not None
        with self._condition:
            if not self._free(owner, owned):
                if self._waiting >= self._max_waiting or (
                    owned and self._waiting_by[owner] >= self._owner_waiting
                ):
                    raise ConversionsBusyError
                self._waiting += 1
                if owned:
                    self._waiting_by[owner] += 1
                try:
                    acquired = self._condition.wait_for(
                        lambda: self._free(owner, owned), timeout=self._wait_seconds
                    )
                finally:
                    self._waiting -= 1
                    if owned:
                        _decrement(self._waiting_by, owner)
                if not acquired:
                    raise ConversionsBusyError
            self._held += 1
            if owned:
                self._held_by[owner] += 1
        try:
            yield
        finally:
            with self._condition:
                self._held -= 1
                if owned:
                    _decrement(self._held_by, owner)
                # Waiters wait for different things (any slot, or one of their owner's).
                self._condition.notify_all()

    def _free(self, owner: Hashable | None, owned: bool) -> bool:
        return self._held < self._slots and (not owned or self._held_by[owner] < self._owner_slots)


def _decrement(counter: Counter[Hashable], owner: Hashable) -> None:
    # Owners at zero are dropped, or every account that ever rendered would stay in memory.
    counter[owner] -= 1
    if not counter[owner]:
        del counter[owner]


@lru_cache
def get_share_conversion_slots() -> ConversionSlots:
    return ConversionSlots(get_settings().share_max_concurrent_conversions)


# The credential in a share link's or a signed URL's path: a share link's payload and a signed
# URL's key id are kept (they help debugging and support), the rest of the token and the
# signature are masked.
_URL_CREDENTIALS = re.compile(
    r"(/shared/[A-Za-z0-9_-]{36}|/signed/[A-Za-z0-9_-]{16}/)[A-Za-z0-9_-]+"
)


def redact_url_credentials(text: str) -> str:
    """`text` with the secret part of any share link or signed URL in it masked."""
    return _URL_CREDENTIALS.sub(r"\1[redacted]", text)


class RedactUrlCredentials(logging.Filter):
    """Masks share links and signed URLs in log records, such as uvicorn's access log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_url_credentials(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_url_credentials(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_url_credentials(arg) if isinstance(arg, str) else arg
                for key, arg in record.args.items()
            }
        return True


# Installed on uvicorn's access logger at startup (app/main.py).
ACCESS_LOG_FILTER = RedactUrlCredentials()
