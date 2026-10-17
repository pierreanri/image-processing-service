"""Signed URLs: transformations carried in a URL that an account's backend signs itself, with
the account's URL signing key, without calling the API (like Cloudinary's signed delivery URLs):

    /signed/{key id}/{signature}/[{options}/]{image id}.{extension}

The key is never stored: it is derived from JWT_SECRET and the key id, a random public id that
the account's row holds (POST /me/url-signing-key) and every URL carries. So a URL's signature is
checked without any I/O, before anything else, and a forged URL costs two HMACs. The signature
is the first 24 base64url characters of the HMAC-SHA256, keyed with the key string as is, of
everything after "/{signature}/". Rotating the key gives the account a new key id, which ends
every URL signed with the old one.

The options are comma-separated tokens that map one to one onto POST /transform's
transformations (see `format_options`), in a single canonical spelling: in the order they are
applied, defaults left out, numbers written one way, watermark text as unpadded base64url of its
UTF-8 bytes, and the expiry last. A URL whose options aren't in that spelling is refused, never
normalized, so each variant has exactly one URL and a signed URL can't be varied.
"""

import base64
import binascii
import hmac
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from pydantic import ValidationError

from app.config import get_settings
from app.imaging import DEFAULT_QUALITY, FORMATS
from app.schemas import TransformationSpec
from app.sharing import ConversionSlots

# Requests that may wait for one of a process's render slots, per slot and in all: each holds one
# of the API's 40 worker threads.
WAITERS_PER_SLOT = 4
MAX_WAITERS = 8

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


@lru_cache
def get_signed_render_slots() -> ConversionSlots:
    """The renders signed URLs may run at once in this process: apart from share links'
    conversions, and at most half of them (one, at least) for any one account."""
    slots = get_settings().signed_url_max_concurrent_renders
    return ConversionSlots(
        slots,
        max_waiting=min(WAITERS_PER_SLOT * slots, MAX_WAITERS),
        owner_slots=max(1, slots // 2),
        owner_waiting=slots,
    )


# --- Options --------------------------------------------------------------------------------------

_FLAGS = ("flip", "mirror", "sharpen", "grayscale", "sepia")
_POSITIONS = ("top-left", "top-right", "bottom-left", "center")
# Lenient on purpose (leading zeros, signs): canonical spellings are enforced by re-encoding.
_INTEGER = re.compile(r"[0-9]{1,9}")
# Unix seconds take 10 digits.
_SECONDS = re.compile(r"[0-9]{1,10}")
_DECIMAL = re.compile(r"-?[0-9]{1,9}(\.[0-9]{1,9})?")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
# Where pydantic reports an error, as the option that set it.
_TOKEN_OF = {
    "crop": "crop_",
    "resize": "w_",
    ("resize", "width"): "w_",
    ("resize", "height"): "h_",
    "rotate": "a_",
    ("filters", "blur"): "blur_",
    ("watermark", "text"): "wm_",
    ("watermark", "size"): "fs_",
    ("watermark", "opacity"): "o_",
    "quality": "q_",
}


class InvalidOptions(ValueError):
    """A signed URL's options are invalid or not in canonical form."""


@dataclass(frozen=True)
class SignedOptions:
    # What to render, its format included.
    spec: TransformationSpec
    # The canonical options without the expiry: one per variant ("" for none).
    transformations: str
    # Unix seconds: the URL works while the time is before this; None: no expiry.
    expires: int | None


def _number(value: float) -> str:
    """A decimal as options write it: at most 2 decimals, no trailing zeros."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_options(spec: TransformationSpec, expires: int | None = None) -> str:
    """The canonical options of `spec` (whose format is the URL's extension), and the expiry."""
    tokens = []
    if (crop := spec.crop) is not None:
        tokens.append(f"crop_{crop.x}_{crop.y}_{crop.width}_{crop.height}")
    if (resize := spec.resize) is not None:
        if resize.width is not None:
            tokens.append(f"w_{resize.width}")
        if resize.height is not None:
            tokens.append(f"h_{resize.height}")
        if resize.width is not None and resize.height is not None and resize.fit != "fill":
            tokens.append(f"c_{resize.fit}")
    if spec.rotate is not None and (angle := _number(spec.rotate % 360)) not in ("0", "360"):
        tokens.append(f"a_{angle}")
    tokens += [name for name in ("flip", "mirror") if getattr(spec, name)]
    if (filters := spec.filters) is not None:
        if filters.blur is not None:
            tokens.append(f"blur_{_number(filters.blur)}")
        tokens += [name for name in ("sharpen", "grayscale", "sepia") if getattr(filters, name)]
    if (watermark := spec.watermark) is not None:
        text = base64.urlsafe_b64encode(watermark.text.encode()).decode().rstrip("=")
        tokens.append(f"wm_{text}")
        if watermark.position != "bottom-right":
            tokens.append(f"g_{watermark.position}")
        if (opacity := round(watermark.opacity * 100)) != 50:
            tokens.append(f"o_{opacity}")
        if watermark.size is not None:
            tokens.append(f"fs_{watermark.size}")
    # Lossless formats ignore quality.
    if spec.quality is not None and spec.format in DEFAULT_QUALITY:
        tokens.append(f"q_{spec.quality}")
    if expires is not None:
        tokens.append(f"exp_{expires}")
    return ",".join(tokens)


def parse_options(options: str | None, image_format: str) -> SignedOptions:
    """What a signed URL's options (None: the URL has none) ask for, rendered as
    `image_format`. Raises InvalidOptions, never repeating values from the URL."""
    values: dict = {"format": image_format}
    expires = None
    if options is not None:
        seen = set()
        for token in options.split(","):
            name, separator, value = token.partition("_")
            if not token:
                raise InvalidOptions("empty option (two commas in a row, or a trailing comma)")
            if name in seen:
                raise InvalidOptions(f"{name} is given twice")
            seen.add(name)
            if name == "exp" and separator:
                if not _SECONDS.fullmatch(value):
                    raise InvalidOptions("exp_: must be Unix seconds from 1 to 9999999999")
                expires = int(value)
            else:
                _parse_token(values, name, separator, value)
        for name in ("g", "o", "fs"):
            if name in seen and "wm" not in seen:
                raise InvalidOptions(f"{name}_ is only allowed with wm_ (a watermark)")
        if "c" in seen and not {"w", "h"} <= seen:
            raise InvalidOptions("c_ is only allowed with both w_ and h_")
        if expires is not None and not 0 < expires <= 9_999_999_999:
            raise InvalidOptions("exp_: must be Unix seconds from 1 to 9999999999")
    try:
        spec = TransformationSpec.model_validate(values)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = tuple(str(part) for part in error["loc"])
        token = _TOKEN_OF.get(location[:2]) or _TOKEN_OF.get(location[0], location[0])
        message = error["msg"].removeprefix("Value error, ")
        raise InvalidOptions(f"{token}: {message}") from None
    canonical = format_options(spec, expires)
    if canonical != (options or ""):
        if not canonical:
            raise InvalidOptions(
                "not in canonical form: these options change nothing, leave the options segment out"
            )
        raise InvalidOptions(f'not in canonical form; sign "{canonical}" instead')
    return SignedOptions(spec, format_options(spec), expires)


def _parse_token(values: dict, name: str, separator: str, value: str) -> None:
    if not separator:
        if name not in _FLAGS:
            raise InvalidOptions(f"unknown option {name}")
        group = values.setdefault("filters", {}) if name not in ("flip", "mirror") else values
        group[name] = True
    elif name == "crop":
        parts = value.split("_")
        if len(parts) != 4:
            raise InvalidOptions("crop_: expected crop_X_Y_WIDTH_HEIGHT")
        x, y, width, height = (_integer(name, part) for part in parts)
        values["crop"] = {"x": x, "y": y, "width": width, "height": height}
    elif name in ("w", "h"):
        values.setdefault("resize", {})["width" if name == "w" else "height"] = _integer(
            name, value
        )
    elif name == "c":
        if value not in ("fill", "contain", "cover"):
            raise InvalidOptions("c_: expected c_contain or c_cover")
        values.setdefault("resize", {})["fit"] = value
    elif name == "a":
        values["rotate"] = _decimal(name, value)
    elif name == "blur":
        values.setdefault("filters", {})["blur"] = _decimal(name, value)
    elif name == "wm":
        values.setdefault("watermark", {})["text"] = _text(value)
    elif name == "g":
        if value not in (*_POSITIONS, "bottom-right"):
            raise InvalidOptions(f"g_: expected one of {', '.join(_POSITIONS)}")
        values.setdefault("watermark", {})["position"] = value
    elif name == "o":
        opacity = _integer(name, value)
        if opacity > 100:
            raise InvalidOptions("o_: expected a percentage from 0 to 100")
        values.setdefault("watermark", {})["opacity"] = opacity / 100
    elif name == "fs":
        values.setdefault("watermark", {})["size"] = _integer(name, value)
    elif name == "q":
        values["quality"] = _integer(name, value)
    elif name == "f":
        raise InvalidOptions("there is no f_: the format is the file extension (…/{image id}.webp)")
    else:
        raise InvalidOptions(f"unknown option {name}_")


def _integer(name: str, value: str) -> int:
    if not _INTEGER.fullmatch(value):
        raise InvalidOptions(f"{name}_: expected a whole number")
    return int(value)


def _decimal(name: str, value: str) -> float:
    if not _DECIMAL.fullmatch(value):
        raise InvalidOptions(f"{name}_: expected a number with at most 2 decimals")
    return float(value)


def _text(value: str) -> str:
    """Watermark text: unpadded base64url of its UTF-8 bytes."""
    if not _BASE64URL.fullmatch(value):
        raise InvalidOptions("wm_: expected the text's UTF-8 bytes as unpadded base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise InvalidOptions("wm_: expected the text's UTF-8 bytes as unpadded base64url") from None
