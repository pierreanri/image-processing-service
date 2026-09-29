"""Image decoding, transformation and encoding with Pillow.

This module has no web or database dependencies so it can be tested (and reused) on its own.
"""

import io
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
    loaded: LoadedImage, spec: TransformationSpec, max_dimension: int
) -> EncodedImage:
    """Apply `spec` to an image and encode the result.

    Order: crop, resize, rotate, flip, mirror, filters, watermark, then encode.
    """
    image = _normalize_mode(loaded.image)

    if spec.crop is not None:
        crop = spec.crop
        if crop.x + crop.width > image.width or crop.y + crop.height > image.height:
            raise TransformationError(
                f"Crop area ({crop.x},{crop.y},{crop.width}x{crop.height}) is outside the "
                f"{image.width}x{image.height} image"
            )
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

    if image.width > max_dimension or image.height > max_dimension:
        raise TransformationError(
            f"Resulting image {image.width}x{image.height} exceeds the maximum dimension "
            f"of {max_dimension}px"
        )

    return encode_image(image, spec.format or loaded.format, spec.quality)


def encode_image(image: Image.Image, image_format: str, quality: int | None = None) -> EncodedImage:
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
        options = {"optimize": True}
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
    width, height = spec.width, spec.height
    if width is None:
        width = max(1, round(image.width * height / image.height))
    elif height is None:
        height = max(1, round(image.height * width / image.width))

    if width > max_dimension or height > max_dimension:
        raise TransformationError(
            f"Resize to {width}x{height} exceeds the maximum dimension of {max_dimension}px"
        )

    size = (width, height)
    both_given = spec.width is not None and spec.height is not None
    if both_given and spec.fit == "contain":
        return ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    if both_given and spec.fit == "cover":
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    return image.resize(size, Image.Resampling.LANCZOS)


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
