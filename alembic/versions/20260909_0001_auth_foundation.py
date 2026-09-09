"""Create the minimal HUMAN user and security-event foundation.

Revision ID: 20260909_0001
Revises:
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("normalized_email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=11), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(normalized_email) BETWEEN 3 AND 254",
            name="ck_users_normalized_email_length",
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name="ck_users_name_length",
        ),
        sa.CheckConstraint(
            "char_length(password_hash) > 0",
            name="ck_users_password_hash_present",
        ),
        sa.CheckConstraint("role IN ('USER', 'ADMIN', 'SUPER_ADMIN')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_users_status"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_users_timestamp_order"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
    )
    op.create_table(
        "user_security_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("subject_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("initiator_type", sa.String(length=18), nullable=False),
        sa.Column("initiator_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initiator_role", sa.String(length=11), nullable=True),
        sa.Column("previous_role", sa.String(length=11), nullable=True),
        sa.Column("new_role", sa.String(length=11), nullable=True),
        sa.Column("previous_status", sa.String(length=8), nullable=True),
        sa.Column("new_status", sa.String(length=8), nullable=True),
        sa.CheckConstraint(
            """
            ((
                    initiator_type = 'AUTHENTICATED_USER'
                    AND initiator_user_id IS NOT NULL
                    AND initiator_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                ) OR (
                    initiator_type IN ('RESET_TOKEN', 'BOOTSTRAP_CLI')
                    AND initiator_user_id IS NULL
                    AND initiator_role IS NULL
                )) IS TRUE
            """,
            name="ck_user_security_events_initiator",
        ),
        sa.CheckConstraint(
            """
            ((
                    event_type = 'INITIAL_SUPER_ADMIN_BOOTSTRAPPED'
                    AND initiator_type = 'BOOTSTRAP_CLI'
                    AND previous_role IS NULL
                    AND new_role = 'SUPER_ADMIN'
                    AND previous_status IS NULL
                    AND new_status = 'ACTIVE'
                ) OR (
                    event_type = 'USER_ROLE_CHANGED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                    AND new_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                    AND previous_role <> new_role
                    AND previous_status IS NULL
                    AND new_status IS NULL
                ) OR (
                    event_type = 'USER_DISABLED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status = 'ACTIVE'
                    AND new_status = 'DISABLED'
                ) OR (
                    event_type = 'USER_ENABLED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status = 'DISABLED'
                    AND new_status = 'ACTIVE'
                ) OR (
                    event_type = 'PASSWORD_CHANGED'
                    AND initiator_type = 'AUTHENTICATED_USER'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status IS NULL
                    AND new_status IS NULL
                ) OR (
                    event_type = 'PASSWORD_RESET'
                    AND initiator_type = 'RESET_TOKEN'
                    AND previous_role IS NULL
                    AND new_role IS NULL
                    AND previous_status IS NULL
                    AND new_status IS NULL
                )) IS TRUE
            """,
            name="ck_user_security_events_shape",
        ),
        sa.ForeignKeyConstraint(["initiator_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("user_security_events")
    op.drop_table("users")
