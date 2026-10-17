import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    __table_args__ = (
        CheckConstraint("storage_quota_bytes >= -1", name="ck_users_storage_quota_bytes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Bytes of images this user may store: NULL means STORAGE_QUOTA_BYTES applies, -1 no limit
    # (see app/quota.py).
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)

    images: Mapped[list["Image"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_owner_id_created_at", "owner_id", "created_at"),
        # Lets a user's storage usage be summed from the index alone (app/quota.py).
        Index("ix_images_owner_id_size", "owner_id", postgresql_include=["size_bytes"]),
    )

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
    # Incremented by DELETE /images/{id}/share-links: share links carry the value they were
    # issued with, and stop working once it changes (see app/sharing.py).
    share_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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
        # Listing a user's unfinished jobs (to catch up on webhook events) reads only those.
        Index(
            "ix_jobs_owner_unfinished",
            "owner_id",
            "created_at",
            "id",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
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


WEBHOOK_EVENT_TYPES = ("job.succeeded", "job.failed")
WEBHOOK_DELIVERY_STATES = ("pending", "delivered", "failed", "canceled")


class WebhookEndpoint(Base):
    """A user's webhook: the URL their job events are sent to (see app/webhooks.py)."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        CheckConstraint(
            "(previous_secret_salt IS NULL) = (previous_secret_expires_at IS NULL)",
            name="ck_webhook_endpoints_previous_secret",
        ),
        # Senders look for the endpoint that is due first.
        Index(
            "ix_webhook_endpoints_due",
            "next_attempt_at",
            postgresql_where=text("next_attempt_at IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    # The signing secret is derived from this and JWT_SECRET, never stored.
    secret_salt: Mapped[bytes] = mapped_column(LargeBinary)
    secret_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # After a rotation, the previous secret also signs requests until it expires.
    previous_secret_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When a sender should next look at this endpoint; NULL while nothing is pending. While a
    # sender holds it, the deadline of its lease.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Identifies the sender holding the endpoint; NULL while none does.
    claim_token: Mapped[uuid.UUID | None] = mapped_column()
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Nothing is sent to the endpoint before this, after a failure.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookDelivery(Base):
    """An event about a finished job, and the state of its delivery to the owner's webhook. Its
    id is the event's id (the webhook-id header)."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            "type IN ('job.succeeded', 'job.failed')", name="ck_webhook_deliveries_type"
        ),
        CheckConstraint(
            "state IN ('pending', 'delivered', 'failed', 'canceled')",
            name="ck_webhook_deliveries_state",
        ),
        # A user's next delivery to attempt.
        Index(
            "ix_webhook_deliveries_pending",
            "user_id",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
        # A user's deliveries to give up on.
        Index(
            "ix_webhook_deliveries_pending_created",
            "user_id",
            "created_at",
            postgresql_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # One event per job, deleted with it.
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True
    )
    # The job's owner (no foreign key: deleting a user deletes their jobs, and so this).
    user_id: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    # Attempts started, including one in progress or interrupted.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # While pending, the earliest time of the next attempt.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(100))
    # When the job finished.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the event was delivered, given up on, or canceled.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UrlSigningKey(Base):
    """An account's URL signing key (see app/signed_urls.py): only its public id is stored, the
    key is derived from it."""

    __tablename__ = "url_signing_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Unique: the key depends on its id alone, so two accounts must never share one.
    key_id: Mapped[str] = mapped_column(String(16), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
