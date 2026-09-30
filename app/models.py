import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    images: Mapped[list["Image"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (Index("ix_images_owner_id_created_at", "owner_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Set when this image was produced by transforming another image.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(10))
    mime_type: Mapped[str] = mapped_column(String(50))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    transformations: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship(back_populates="images")


JOB_STATUSES = ("queued", "running", "succeeded", "failed")


class Job(Base):
    """A transformation run in the background (POST /images/{id}/transform with
    `Prefer: respond-async`). See app/jobs.py for how workers claim and finish jobs."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_jobs_status"
        ),
        Index("ix_jobs_owner_id_created_at", "owner_id", "created_at"),
        # The claim query only looks at unfinished jobs.
        Index(
            "ix_jobs_claimable",
            "available_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source_image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL")
    )
    result_image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL")
    )
    transformations: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    status: Mapped[str] = mapped_column(String(10), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # When a queued job may run; while it runs, the deadline of the worker's lease on it.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Identifies the current claim, so a worker whose lease expired can't finish the job.
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    error_status: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
