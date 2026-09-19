"""Persist password-reset challenges and digest-only credentials.

Revision ID: 20260919_0018
Revises: 20260918_0017
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260919_0018"
down_revision: str | None = "20260918_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = """
        CREATE TABLE password_reset_challenges (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            status varchar(9) NOT NULL DEFAULT 'ACTIVE',
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            completed_at timestamptz,
            revoked_at timestamptz,
            CONSTRAINT ck_password_reset_challenge_state CHECK (
                (status IN ('ACTIVE', 'EXPIRED') AND completed_at IS NULL AND revoked_at IS NULL) OR
                (status = 'COMPLETED' AND completed_at IS NOT NULL AND revoked_at IS NULL) OR
                (status = 'REVOKED' AND completed_at IS NULL AND revoked_at IS NOT NULL)
            ),
            CONSTRAINT ck_password_reset_challenge_times CHECK (
                expires_at > created_at AND
                (completed_at IS NULL OR
                 (completed_at >= created_at AND completed_at < expires_at)) AND
                (revoked_at IS NULL OR revoked_at >= created_at)
            )
        );
        CREATE UNIQUE INDEX uq_password_reset_challenges_active_user
            ON password_reset_challenges(user_id) WHERE status = 'ACTIVE';
        CREATE INDEX ix_password_reset_challenges_user_id ON password_reset_challenges(user_id, id);
        CREATE TABLE password_reset_tokens (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            challenge_id uuid NOT NULL REFERENCES password_reset_challenges(id) ON DELETE RESTRICT,
            digest bytea NOT NULL,
            issued_at timestamptz NOT NULL,
            used_at timestamptz,
            CONSTRAINT uq_password_reset_tokens_digest UNIQUE(digest),
            CONSTRAINT ck_password_reset_token_digest CHECK (octet_length(digest) = 32),
            CONSTRAINT ck_password_reset_token_used CHECK (used_at IS NULL OR used_at >= issued_at)
        );
        CREATE INDEX ix_password_reset_tokens_challenge_id
            ON password_reset_tokens(challenge_id, id);
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("password_reset_challenges")
