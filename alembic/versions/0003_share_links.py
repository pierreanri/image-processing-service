"""Add images.share_generation for revoking share links.

Downgrading drops the counters, and upgrading again starts them all at 0, which revives every
revoked link that hasn't expired yet: afterwards, change JWT_SECRET (or see the README's note on
restoring a backup).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A constant default: PostgreSQL 11+ adds the column without rewriting the table.
    op.add_column(
        "images",
        sa.Column("share_generation", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("images", "share_generation")
