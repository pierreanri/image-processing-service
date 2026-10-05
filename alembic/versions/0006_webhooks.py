"""Create the webhook_endpoints and webhook_deliveries tables for webhooks.

Both tables are new, so their indexes are built with them. Downgrading drops every webhook and
every event not yet delivered; upgrading again gives users new signing secrets once they register
again.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("secret_salt", sa.LargeBinary(), nullable=False),
        sa.Column(
            "secret_created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("previous_secret_salt", sa.LargeBinary()),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paused_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(previous_secret_salt IS NULL) = (previous_secret_expires_at IS NULL)",
            name="ck_webhook_endpoints_previous_secret",
        ),
    )
    op.create_index(
        "ix_webhook_endpoints_due",
        "webhook_endpoints",
        ["next_attempt_at"],
        postgresql_where=sa.text("next_attempt_at IS NOT NULL"),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("state", sa.String(10), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_status_code", sa.Integer()),
        sa.Column("last_error", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "type IN ('job.succeeded', 'job.failed')", name="ck_webhook_deliveries_type"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'delivered', 'failed', 'canceled')",
            name="ck_webhook_deliveries_state",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_pending",
        "webhook_deliveries",
        ["user_id", "next_attempt_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_webhook_deliveries_pending_created",
        "webhook_deliveries",
        ["user_id", "created_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_pending_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_pending", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_endpoints_due", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
