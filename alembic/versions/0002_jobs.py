"""Create the jobs table for background transformations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source_image_id", sa.Uuid(), sa.ForeignKey("images.id", ondelete="SET NULL")),
        sa.Column("result_image_id", sa.Uuid(), sa.ForeignKey("images.id", ondelete="SET NULL")),
        sa.Column("transformations", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("error_status", sa.Integer()),
        sa.Column("error_detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_jobs_status"
        ),
    )
    op.create_index("ix_jobs_owner_id_created_at", "jobs", ["owner_id", "created_at"])
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["available_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_index("ix_jobs_owner_id_created_at", table_name="jobs")
    op.drop_table("jobs")
