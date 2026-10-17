"""Image decoding, transformation and encoding with Pillow.

This module has no web or database dependencies so it can be tested (and reused) on its own.
"""

import io
import math
import warnings
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from app.schemas import FiltersSpec, ResizeSpec, TransformationSpec, WatermarkSpec


@dataclass(frozen=True)
class FormatInfo:
    pillow_name: str
    mime_type: str
    extension: str
    supports_alpha: bool


FORMATS: dict[str, FormatInfo] = {
    "jpeg": FormatInfo("JPEG", "image/jpeg", "jpg", supports_alpha=False),
    "png": FormatInfo("PNG", "image/png", "png", supports_alpha=True),
    "webp": FormatInfo("WEBP", "image/webp", "webp", supports_alpha=True),
    "gif": FormatInfo("GIF", "image/gif", "gif", supports_alpha=True),
    "bmp": FormatInfo("BMP", "image/bmp", "bmp", supports_alpha=False),
    "tiff": FormatInfo("TIFF", "image/tiff", "tiff", supports_alpha=True),
}

# Pillow format names that map onto one of our formats. MPO is the multi-picture JPEG
# variant many phone cameras produce.
_PILLOW_TO_FORMAT = {info.pillow_name: name for name, info in FORMATS.items()} | {"MPO": "jpeg"}

DEFAULT_QUALITY = {"jpeg": 85, "webp": 80}
# The largest width/height Pillow's encoders accept; beyond these, saving fails.
ENCODER_MAX_DIMENSION = {"jpeg": 65_500, "webp": 16_383, "gif": 65_535}


def max_output_dimension(image_format: str, max_dimension: int) -> int:
    """The largest width/height a transformation may produce in `image_format`: MAX_DIMENSION, or
    less if the format's encoder can't go that far."""
    return min(max_dimension, ENCODER_MAX_DIMENSION.get(image_format, max_dimension))


class ImageProcessingError(Exception):
    status_code = 400


class InvalidImageError(ImageProcessingError):
    status_code = 400


class UnsupportedFormatError(ImageProcessingError):
    status_code = 415


class TransformationError(ImageProcessingError):
    status_code = 422


@dataclass(frozen=True)
class LoadedImage:
    image: Image.Image
    format: str


@dataclass(frozen=True)
class EncodedImage:
    data: bytes
    format: str
    width: int
    height: int

    @property
    def mime_type(self) -> str:
        return FORMATS[self.format].mime_type

    @property
    def extension(self) -> str:
        return FORMATS[self.format].extension


def load_image(data: bytes, max_pixels: int) -> LoadedImage:
    """Decode and validate image bytes.

    The format is detected from the file contents, never from the client's filename or
    content type. The returned image is fully loaded and has its EXIF orientation applied.
    """
    try:
        with warnings.catch_warnings():
            # Pillow warns before raising DecompressionBombError; we enforce our own limit.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                image_format = _PILLOW_TO_FORMAT.get(probe.format or "")
                if image_format is None:
                    raise UnsupportedFormatError(
                        f"Unsupported image format {probe.format!r}; "
                        f"supported formats: {', '.join(FORMATS)}"
                    )
                if probe.width * probe.height > max_pixels:
                    raise InvalidImageError(
                        f"Image is too large ({probe.width}x{probe.height} pixels)"
                    )
                probe.verify()

            # verify() leaves the image unusable, so decode again for real.
            image = Image.open(io.BytesIO(data))
            image.load()
    except Image.DecompressionBombError:
        raise InvalidImageError("Image is too large") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise InvalidImageError("File is not a valid image") from None

    image = ImageOps.exif_transpose(image)
    return LoadedImage(image=image, format=image_format)


def apply_transformations(
    loaded: LoadedImage, spec: TransformationSpec, max_dimension: int, *, fast: bool = False
) -> EncodedImage:
    """Apply `spec` to an image and encode the result (`fast`: see encode_image).

    Order: crop, resize, rotate, flip, mirror, filters, watermark, then encode.
    """
    image = _normalize_mode(loaded.image)
    image_format = spec.format or loaded.format
    # Refuses what would fail on its size before allocating anything for it: a rotation's
    # expanded canvas alone can take gigabytes.
    predict_output_size(image.width, image.height, spec, image_format, max_dimension)

    if spec.crop is not None:
        crop = spec.crop
        image = image.crop((crop.x, crop.y, crop.x + crop.width, crop.y + crop.height))

    if spec.resize is not None:
        image = _resize(image, spec.resize, max_dimension)

    if spec.rotate:
        image = _rotate(image, spec.rotate)

    if spec.flip:
        image = ImageOps.flip(image)

    if spec.mirror:
        image = ImageOps.mirror(image)

    if spec.filters is not None:
        image = _apply_filters(image, spec.filters)

    if spec.watermark is not None:
        image = _watermark(image, spec.watermark)

    return encode_image(image, image_format, spec.quality, fast=fast)


def predict_output_size(
    width: int, height: int, spec: TransformationSpec, image_format: str, max_dimension: int
) -> tuple[int, int]:
    """The size of what apply_transformations makes of a `width`x`height` image in
    `image_format`, or the TransformationError it raises for the size of its steps (crop,
    resize, the result), computed without touching a pixel."""
    if spec.crop is not None:
        crop = spec.crop
        if crop.x + crop.width > width or crop.y + crop.height > height:
            raise TransformationError(
                f"Crop area ({crop.x},{crop.y},{crop.width}x{crop.height}) is outside the "
                f"{width}x{height} image"
            )
        width, height = crop.width, crop.height
    if spec.resize is not None:
        width, height = _resized_size(width, height, spec.resize, max_dimension)
    if spec.rotate:
        width, height = _rotated_size(width, height, spec.rotate)
    # Flips, filters and watermarks keep the size.
    limit = max_output_dimension(image_format, max_dimension)
    if width > limit or height > limit:
        raise TransformationError(
            f"Resulting image {width}x{height} exceeds the maximum dimension of {limit}px"
            + ("" if limit == max_dimension else f" for {image_format}")
        )
    return width, height


def encode_image(
    image: Image.Image, image_format: str, quality: int | None = None, *, fast: bool = False
) -> EncodedImage:
    """Encode `image`. Stored images are compressed as much as the format allows; `fast` is for
    images encoded on every request (conversions, signed URLs), where an optimized PNG of a
    photo can take half a minute: the same pixels in a slightly larger file, many times faster."""
    info = FORMATS[image_format]
    if not info.supports_alpha and _has_alpha(image):
        image = _flatten(image)

    options: dict = {}
    if image_format == "jpeg":
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        options = {"quality": quality or DEFAULT_QUALITY["jpeg"], "optimize": True}
    elif image_format == "webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if _has_alpha(image) else "RGB")
        options = {"quality": quality or DEFAULT_QUALITY["webp"], "method": 4}
    elif image_format == "png":
        options = {"compress_level": 3} if fast else {"optimize": True}
    elif image_format == "tiff":
        options = {"compression": "tiff_lzw"}

    buffer = io.BytesIO()
    image.save(buffer, format=info.pillow_name, **options)
    return EncodedImage(
        data=buffer.getvalue(), format=image_format, width=image.width, height=image.height
    )


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )


def _normalize_mode(image: Image.Image) -> Image.Image:
    """Convert palette, CMYK, 16-bit etc. images to RGB or RGBA so every operation works."""
    if image.mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if _has_alpha(image) else "RGB")


def _flatten(image: Image.Image) -> Image.Image:
    """Composite an image with transparency onto a white background."""
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    flattened = Image.alpha_composite(background, rgba).convert("RGB")
    return flattened.convert("L") if image.mode == "LA" else flattened


def _resize(image: Image.Image, spec: ResizeSpec, max_dimension: int) -> Image.Image:
    size = _resized_size(image.width, image.height, spec, max_dimension)
    if spec.width is not None and spec.height is not None and spec.fit == "cover":
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    return image.resize(size, Image.Resampling.LANCZOS)


def _resize_target(
    width: int, height: int, spec: ResizeSpec, max_dimension: int
) -> tuple[int, int]:
    """The box a resize of a `width`x`height` image aims at."""
    target_width, target_height = spec.width, spec.height
    if target_width is None:
        target_width = max(1, round(width * target_height / height))
    elif target_height is None:
        target_height = max(1, round(height * target_width / width))
    if target_width > max_dimension or target_height > max_dimension:
        raise TransformationError(
            f"Resize to {target_width}x{target_height} exceeds the maximum dimension of "
            f"{max_dimension}px"
        )
    return target_width, target_height


def _resized_size(width: int, height: int, spec: ResizeSpec, max_dimension: int) -> tuple[int, int]:
    """The size _resize gives a `width`x`height` image."""
    size = _resize_target(width, height, spec, max_dimension)
    if spec.width is None or spec.height is None or spec.fit != "contain":
        return size
    # As ImageOps.contain computes it, but never 0 pixels (for a very wide or tall image).
    image_ratio, box_ratio = width / height, size[0] / size[1]
    if image_ratio > box_ratio:
        return size[0], max(1, round(height / width * size[0]))
    if image_ratio < box_ratio:
        return max(1, round(width / height * size[1])), size[1]
    return size


def _rotated_size(width: int, height: int, degrees: float) -> tuple[int, int]:
    """The size _rotate gives a `width`x`height` image: the bounding box of its rotated corners,
    computed exactly as Image.rotate(expand=True) does."""
    angle = -degrees % 360
    if angle % 90 == 0:
        return (height, width) if angle % 180 == 90 else (width, height)
    radians = -math.radians(angle % 360.0)
    a, b = round(math.cos(radians), 15), round(math.sin(radians), 15)
    d, e = round(-math.sin(radians), 15), round(math.cos(radians), 15)
    center_x, center_y = width / 2, height / 2
    c = a * -center_x + b * -center_y + 0.0 + center_x
    f = d * -center_x + e * -center_y + 0.0 + center_y
    corners = ((0, 0), (width, 0), (width, height), (0, height))
    xs = [a * x + b * y + c for x, y in corners]
    ys = [d * x + e * y + f for x, y in corners]
    return (
        math.ceil(max(xs)) - math.floor(min(xs)),
        math.ceil(max(ys)) - math.floor(min(ys)),
    )


def _rotate(image: Image.Image, degrees: float) -> Image.Image:
    # Pillow rotates counter-clockwise; the API takes clockwise degrees.
    angle = -degrees % 360
    if angle % 90 == 0:
        # Right angles are lossless and need no fill.
        return image.rotate(angle, expand=True)
    rgba = image.convert("RGBA")
    return rgba.rotate(
        angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0)
    )


def _apply_filters(image: Image.Image, spec: FiltersSpec) -> Image.Image:
    if spec.blur:
        image = image.filter(ImageFilter.GaussianBlur(spec.blur))
    if spec.sharpen:
        image = image.filter(ImageFilter.SHARPEN)
    if spec.grayscale:
        image = image.convert("LA" if _has_alpha(image) else "L")
    if spec.sepia:
        image = _sepia(image)
    return image


_SEPIA_MATRIX = (
    0.393, 0.769, 0.189, 0,
    0.349, 0.686, 0.168, 0,
    0.272, 0.534, 0.131, 0,
)  # fmt: skip


def _sepia(image: Image.Image) -> Image.Image:
    alpha = image.getchannel("A") if _has_alpha(image) else None
    sepia = image.convert("RGB").convert("RGB", _SEPIA_MATRIX)
    if alpha is not None:
        sepia.putalpha(alpha)
    return sepia


def _watermark(image: Image.Image, spec: WatermarkSpec) -> Image.Image:
    original_mode = image.mode
    base = image.convert("RGBA")

    size = spec.size or max(12, min(base.size) // 20)
    font = ImageFont.load_default(size=size)
    stroke = max(1, size // 15)
    alpha = round(255 * spec.opacity)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = draw.textbbox((0, 0), spec.text, font=font, stroke_width=stroke)
    text_width, text_height = right - left, bottom - top
    margin = max(4, size // 2)

    if spec.position == "center":
        x = (base.width - text_width) // 2
        y = (base.height - text_height) // 2
    else:
        vertical, horizontal = spec.position.split("-")
        x = margin if horizontal == "left" else base.width - text_width - margin
        y = margin if vertical == "top" else base.height - text_height - margin

    draw.text(
        (x - left, y - top),
        spec.text,
        font=font,
        fill=(255, 255, 255, alpha),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, alpha),
    )
    watermarked = Image.alpha_composite(base, overlay)
    return watermarked if original_mode == "RGBA" else watermarked.convert(original_mode)
