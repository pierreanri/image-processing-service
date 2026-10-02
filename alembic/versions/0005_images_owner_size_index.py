"""Add an index to sum a user's storage usage (app/quota.py) from the index alone.

It is built concurrently, outside the migration's transaction, so uploads aren't blocked while it
builds. Entering that block commits the earlier migrations first, so if the build fails or is
interrupted the database stays at 0004: run `alembic upgrade head` again, which first drops the
invalid index a failed build leaves.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_images_owner_id_size",
            table_name="images",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_images_owner_id_size",
            "images",
            ["owner_id"],
            postgresql_include=["size_bytes"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_images_owner_id_size", table_name="images", postgresql_concurrently=True)
