import io

import pytest
from PIL import Image

from app.imaging import (
    FORMATS,
    InvalidImageError,
    TransformationError,
    UnsupportedFormatError,
    apply_transformations,
    load_image,
)
from app.schemas import TransformationSpec
from tests.utils import make_image_bytes

MAX_PIXELS = 50_000_000
MAX_DIMENSION = 10_000


def load(data: bytes):
    return load_image(data, MAX_PIXELS)


def transform(data: bytes, **spec):
    return apply_transformations(load(data), TransformationSpec(**spec), MAX_DIMENSION)


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def two_tone(size=(40, 20)) -> bytes:
    """Left half red, right half blue: makes orientation changes observable."""
    image = Image.new("RGB", size, "red")
    image.paste("blue", (size[0] // 2, 0, size[0], size[1]))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Loading --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"])
def test_load_detects_format_from_content(fmt):
    loaded = load(make_image_bytes(fmt))

    assert loaded.format == fmt.lower()
    assert loaded.image.size == (64, 48)


def test_load_rejects_non_images():
    with pytest.raises(InvalidImageError):
        load(b"definitely not an image")


def test_load_rejects_truncated_images():
    with pytest.raises(InvalidImageError):
        load(make_image_bytes("PNG")[:40])


def test_load_rejects_unsupported_formats():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PCX")

    with pytest.raises(UnsupportedFormatError):
        load(buffer.getvalue())


def test_load_rejects_images_over_pixel_limit():
    with pytest.raises(InvalidImageError, match="too large"):
        load_image(make_image_bytes(size=(100, 100)), max_pixels=9_999)


def test_load_applies_exif_orientation():
    image = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display.
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    assert load(buffer.getvalue()).image.size == (20, 40)


# --- Geometry -------------------------------------------------------------------------------------


def test_crop():
    result = transform(
        make_image_bytes(size=(100, 80)), crop={"x": 10, "y": 5, "width": 30, "height": 20}
    )

    assert (result.width, result.height) == (30, 20)
    assert decode(result.data).size == (30, 20)


def test_crop_outside_image_is_rejected():
    with pytest.raises(TransformationError):
        transform(
            make_image_bytes(size=(100, 80)), crop={"x": 90, "y": 0, "width": 20, "height": 20}
        )


def test_resize_to_exact_size():
    result = transform(make_image_bytes(size=(100, 50)), resize={"width": 30, "height": 30})

    assert (result.width, result.height) == (30, 30)


def test_resize_width_only_keeps_aspect_ratio():
    result = transform(make_image_bytes(size=(100, 50)), resize={"width": 40})

    assert (result.width, result.height) == (40, 20)


def test_resize_height_only_keeps_aspect_ratio():
    result = transform(make_image_bytes(size=(100, 50)), resize={"height": 10})

    assert (result.width, result.height) == (20, 10)


def test_resize_contain_fits_inside_box():
    result = transform(
        make_image_bytes(size=(100, 50)), resize={"width": 40, "height": 40, "fit": "contain"}
    )

    assert (result.width, result.height) == (40, 20)


def test_resize_cover_fills_box():
    result = transform(
        make_image_bytes(size=(100, 50)), resize={"width": 40, "height": 40, "fit": "cover"}
    )

    assert (result.width, result.height) == (40, 40)


def test_resize_above_max_dimension_is_rejected():
    with pytest.raises(TransformationError):
        transform(make_image_bytes(), resize={"width": MAX_DIMENSION + 1})


@pytest.mark.parametrize(("format", "limit"), [("webp", 16_383), ("jpeg", 65_500), ("gif", 65_535)])
def test_formats_whose_encoders_cap_the_size_are_rejected_not_crashed(format, limit):
    """With MAX_DIMENSION raised beyond what the encoder takes, saving would fail with a 500."""
    wide = load(make_image_bytes(size=(limit + 1, 1)))

    with pytest.raises(TransformationError, match=f"maximum dimension of {limit}px for {format}"):
        apply_transformations(wide, TransformationSpec(format=format), limit + 100)

    # The limit itself is fine, and so is the same size in a format without such a cap.
    exact = load(make_image_bytes(size=(limit, 1)))
    assert (
        apply_transformations(exact, TransformationSpec(format=format), limit + 100).width == limit
    )
    png = apply_transformations(wide, TransformationSpec(format="png"), limit + 100)
    assert png.width == limit + 1


def test_rotate_right_angle_swaps_dimensions():
    result = transform(two_tone(), rotate=90)

    image = decode(result.data).convert("RGB")
    assert image.size == (20, 40)
    # Clockwise: the red left half ends up on top.
    assert image.getpixel((10, 5)) == (255, 0, 0)
    assert image.getpixel((10, 35)) == (0, 0, 255)


def test_rotate_arbitrary_angle_expands_with_transparent_corners():
    result = transform(make_image_bytes(size=(40, 40)), rotate=45)

    image = decode(result.data)
    assert image.width > 40 and image.height > 40
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0


def test_flip_is_vertical():
    image = Image.new("RGB", (10, 10), "red")
    image.paste("blue", (0, 5, 10, 10))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    result = decode(transform(buffer.getvalue(), flip=True).data).convert("RGB")

    assert result.getpixel((5, 0)) == (0, 0, 255)


def test_mirror_is_horizontal():
    result = decode(transform(two_tone(), mirror=True).data).convert("RGB")

    assert result.getpixel((0, 10)) == (0, 0, 255)
    assert result.getpixel((39, 10)) == (255, 0, 0)


def test_transformations_apply_in_documented_order():
    # Crop happens before resize, so the crop coordinates refer to the original image.
    result = transform(
        make_image_bytes(size=(200, 100)),
        crop={"x": 0, "y": 0, "width": 100, "height": 100},
        resize={"width": 50},
        rotate=90,
    )

    assert (result.width, result.height) == (50, 50)


# --- Filters and watermark ------------------------------------------------------------------------


def test_grayscale():
    result = transform(make_image_bytes(color=(200, 30, 30)), filters={"grayscale": True})

    image = decode(result.data)
    assert image.mode == "L"


def test_sepia_gives_warm_tone():
    result = transform(make_image_bytes(color=(128, 128, 128)), filters={"sepia": True})

    r, g, b = decode(result.data).convert("RGB").getpixel((0, 0))
    assert r > g > b


def test_blur_and_sharpen_keep_size():
    result = transform(two_tone(), filters={"blur": 2, "sharpen": True})

    assert (result.width, result.height) == (40, 20)


def test_blur_softens_edges():
    result = decode(transform(two_tone(), filters={"blur": 3}).data).convert("RGB")

    r, _, b = result.getpixel((19, 10))
    assert 0 < r < 255 and 0 < b < 255


def test_watermark_changes_pixels_in_position():
    original = make_image_bytes(size=(200, 100), color=(40, 40, 40))

    result = decode(
        transform(original, watermark={"text": "Hello", "position": "top-left", "opacity": 1}).data
    ).convert("RGB")

    top_left = result.crop((0, 0, 100, 50)).getcolors(maxcolors=100_000)
    bottom_right = result.crop((100, 50, 200, 100)).getcolors(maxcolors=100_000)
    assert len(top_left) > 1
    assert len(bottom_right) == 1


# --- Encoding -------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_convert_to_every_format(fmt):
    result = transform(make_image_bytes("PNG", mode="RGBA", color=(255, 0, 0, 128)), format=fmt)

    assert result.format == fmt
    assert decode(result.data).format == FORMATS[fmt].pillow_name


def test_jpeg_output_flattens_transparency_onto_white():
    result = transform(make_image_bytes("PNG", mode="RGBA", color=(0, 0, 0, 0)), format="jpeg")

    image = decode(result.data)
    assert image.mode == "RGB"
    assert all(channel > 245 for channel in image.getpixel((0, 0)))


def test_lower_quality_produces_smaller_jpeg():
    noisy = Image.effect_noise((200, 200), 64).convert("RGB")
    buffer = io.BytesIO()
    noisy.save(buffer, format="PNG")

    high = transform(buffer.getvalue(), format="jpeg", quality=95)
    low = transform(buffer.getvalue(), format="jpeg", quality=20)

    assert len(low.data) < len(high.data)


def test_output_keeps_source_format_by_default():
    assert transform(make_image_bytes("WEBP"), mirror=True).format == "webp"


def test_format_aliases_are_normalized():
    assert TransformationSpec(format="JPG").format == "jpeg"
    assert TransformationSpec(format="tif").format == "tiff"


def test_empty_spec_is_rejected():
    with pytest.raises(ValueError, match="at least one transformation"):
        TransformationSpec()


def test_unknown_keys_are_rejected():
    with pytest.raises(ValueError):
        TransformationSpec(filters={"grayscal": True})
