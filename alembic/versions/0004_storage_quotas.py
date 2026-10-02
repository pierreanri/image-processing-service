"""Add users.storage_quota_bytes for per-user storage quotas.

Downgrading drops every user's own limit (set with python -m app.quota).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_users_storage_quota_bytes", "users", "storage_quota_bytes >= -1")


def downgrade() -> None:
    op.drop_constraint("ck_users_storage_quota_bytes", "users", type_="check")
    op.drop_column("users", "storage_quota_bytes")
