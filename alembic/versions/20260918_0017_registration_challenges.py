"""Persist registration challenges and digest-only email credentials.

Revision ID: 20260918_0017
Revises: 20260918_0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260918_0017"
down_revision: str | None = "20260918_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = """
        CREATE TABLE registration_challenges (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            normalized_email text NOT NULL,
            status varchar(9) NOT NULL DEFAULT 'ACTIVE',
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            completed_at timestamptz,
            CONSTRAINT ck_registration_challenge_email CHECK (
                char_length(normalized_email) BETWEEN 3 AND 254
            ),
            CONSTRAINT ck_registration_challenge_state CHECK (
                (status IN ('ACTIVE', 'EXPIRED') AND completed_at IS NULL) OR
                (status = 'COMPLETED' AND completed_at IS NOT NULL)
            ),
            CONSTRAINT ck_registration_challenge_times CHECK (
                expires_at > created_at AND
                (completed_at IS NULL OR
                 (completed_at >= created_at AND completed_at < expires_at))
            )
        );
        CREATE UNIQUE INDEX uq_registration_challenges_active_email
            ON registration_challenges(normalized_email) WHERE status = 'ACTIVE';
        CREATE TABLE registration_tokens (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            challenge_id uuid NOT NULL REFERENCES registration_challenges(id) ON DELETE RESTRICT,
            digest bytea NOT NULL,
            issued_at timestamptz NOT NULL,
            used_at timestamptz,
            CONSTRAINT uq_registration_tokens_digest UNIQUE(digest),
            CONSTRAINT ck_registration_token_digest CHECK (octet_length(digest) = 32),
            CONSTRAINT ck_registration_token_used CHECK (used_at IS NULL OR used_at >= issued_at)
        );
        CREATE INDEX ix_registration_tokens_challenge_id
            ON registration_tokens(challenge_id, id);
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("registration_tokens")
    op.drop_table("registration_challenges")
