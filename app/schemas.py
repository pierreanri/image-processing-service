import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(max_length=50)
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.")


class RegisterResponse(TokenResponse):
    user: UserOut


# --- Images -------------------------------------------------------------------------------------

FORMAT_ALIASES = {"jpg": "jpeg", "tif": "tiff"}


def _normalize_format(value: object) -> object:
    if isinstance(value, str):
        value = value.strip().lower()
        return FORMAT_ALIASES.get(value, value)
    return value


ImageFormat = Annotated[
    Literal["jpeg", "png", "webp", "gif", "bmp", "tiff"], BeforeValidator(_normalize_format)
]


class StrictModel(BaseModel):
    # Reject unknown keys so typos like "grayscal" fail loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


class CropSpec(StrictModel):
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ResizeSpec(StrictModel):
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fit: Literal["fill", "contain", "cover"] = Field(
        default="fill",
        description=(
            "Only used when both width and height are given. fill: stretch to the exact size; "
            "contain: scale to fit inside the box, keeping aspect ratio; cover: scale and "
            "center-crop to exactly fill the box."
        ),
    )

    @model_validator(mode="after")
    def _require_a_dimension(self) -> "ResizeSpec":
        if self.width is None and self.height is None:
            raise ValueError("resize needs width, height or both")
        return self


class FiltersSpec(StrictModel):
    grayscale: bool = False
    sepia: bool = False
    blur: float | None = Field(default=None, gt=0, le=50, description="Gaussian blur radius.")
    sharpen: bool = False


class WatermarkSpec(StrictModel):
    text: str = Field(min_length=1, max_length=100)
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"] = (
        "bottom-right"
    )
    opacity: float = Field(default=0.5, ge=0, le=1)
    size: int | None = Field(
        default=None, ge=8, le=500, description="Font size in px; scales with the image if unset."
    )


class TransformationSpec(StrictModel):
    """Transformations are applied in this order: crop, resize, rotate, flip, mirror, filters,
    watermark; the result is then encoded with `format` and `quality`."""

    crop: CropSpec | None = None
    resize: ResizeSpec | None = None
    rotate: float | None = Field(default=None, ge=-360, le=360, description="Degrees, clockwise.")
    flip: bool = Field(default=False, description="Flip vertically (top to bottom).")
    mirror: bool = Field(default=False, description="Mirror horizontally (left to right).")
    filters: FiltersSpec | None = None
    watermark: WatermarkSpec | None = None
    format: ImageFormat | None = Field(
        default=None, description="Output format; defaults to the source format."
    )
    quality: int | None = Field(
        default=None, ge=1, le=100, description="Compression quality for JPEG and WebP."
    )

    @model_validator(mode="after")
    def _require_an_operation(self) -> "TransformationSpec":
        if not self.model_dump(exclude_defaults=True):
            raise ValueError("at least one transformation is required")
        return self


class TransformRequest(StrictModel):
    transformations: TransformationSpec


class ImageOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None = Field(
        description="The image this one was transformed from, if any."
    )
    url: str = Field(description="Where to download the image bytes.")
    original_filename: str
    format: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    transformations: dict[str, Any] | None = Field(
        description="The transformations that produced this image, if any."
    )
    created_at: datetime


class ImageList(BaseModel):
    items: list[ImageOut]
    page: int
    limit: int
    total: int
    pages: int


class ShareLinkRequest(StrictModel):
    format: ImageFormat | None = Field(
        default=None, description="Serve the image converted to this format."
    )
    quality: int | None = Field(default=None, ge=1, le=100, description="JPEG/WebP quality.")
    expires_in: int | None = Field(
        default=None, gt=0, description="Lifetime in seconds (default: 1 day)."
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="When the link stops working, instead of `expires_in`. The same image, "
        "variant and expiry always give the same URL.",
    )

    @model_validator(mode="after")
    def _one_expiry(self) -> "ShareLinkRequest":
        if self.expires_in is not None and self.expires_at is not None:
            raise ValueError("give expires_in or expires_at, not both")
        return self


class ShareLinkOut(BaseModel):
    url: str = Field(
        description="Downloads the image without a token until the link expires or is revoked."
    )
    image_id: uuid.UUID
    format: str
    mime_type: str
    quality: int | None = Field(description="null: the format's default, or a lossless format.")
    expires_at: datetime


class JobError(BaseModel):
    status_code: int = Field(description="The HTTP status the synchronous request would have had.")
    detail: str


class JobOut(BaseModel):
    id: uuid.UUID
    url: str = Field(description="Where to poll this job.")
    status: Literal["queued", "running", "succeeded", "failed"]
    source_image_id: uuid.UUID | None = Field(
        description="The image being transformed (null once it has been deleted)."
    )
    transformations: dict[str, Any]
    attempts: int = Field(description="How many times a worker has started this job.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: ImageOut | None = Field(
        description="The new image, once the job has succeeded (null if it was since deleted)."
    )
    error: JobError | None = Field(
        description="Why the job failed, or the last error of a job waiting to be retried."
    )
