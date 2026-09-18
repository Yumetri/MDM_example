"""Index all-user and role-filtered chronological keyset queries.

Revision ID: 20260918_0016
Revises: 20260915_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0016"
down_revision: str | None = "20260915_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_users_created_at_id", "users", [sa.text("created_at DESC"), sa.text("id DESC")]
    )
    op.create_index(
        "ix_users_role_created_at_id",
        "users",
        ["role", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_users_role_created_at_id", table_name="users")
    op.drop_index("ix_users_created_at_id", table_name="users")
