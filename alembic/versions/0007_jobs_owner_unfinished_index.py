"""Add an index for listing a user's unfinished jobs (GET /jobs?status=queued&status=running).

Without it, that listing reads every job the user has kept. It is built concurrently, outside
the migration's transaction, so jobs aren't blocked while it builds. Entering that block commits
the earlier migrations first, so if the build fails or is interrupted the database stays at 0006:
run `alembic upgrade head` again, which first drops the invalid index a failed build leaves.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_jobs_owner_unfinished",
            table_name="jobs",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_jobs_owner_unfinished",
            "jobs",
            ["owner_id", "created_at", "id"],
            postgresql_where=sa.text("status IN ('queued', 'running')"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_jobs_owner_unfinished", table_name="jobs", postgresql_concurrently=True)
