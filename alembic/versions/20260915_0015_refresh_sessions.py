"""Persist refresh families and digest-only rotation records.

Revision ID: 20260915_0015
Revises: 20260915_0014
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0015"
down_revision: str | None = "20260915_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    statements = """
        CREATE TABLE refresh_families (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            status varchar(7) NOT NULL DEFAULT 'ACTIVE',
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT ck_refresh_family_state CHECK (
                (status = 'ACTIVE' AND revoked_at IS NULL) OR
                (status = 'REVOKED' AND revoked_at IS NOT NULL)
            ),
            CONSTRAINT ck_refresh_family_times CHECK (
                expires_at > created_at AND updated_at >= created_at AND
                (revoked_at IS NULL OR revoked_at >= created_at)
            )
        );
        CREATE UNIQUE INDEX uq_refresh_families_active_user
            ON refresh_families(user_id) WHERE status = 'ACTIVE';
        CREATE INDEX ix_refresh_families_user_id ON refresh_families(user_id, id);
        CREATE TABLE refresh_tokens (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            family_id uuid NOT NULL REFERENCES refresh_families(id) ON DELETE RESTRICT,
            digest bytea NOT NULL,
            issued_at timestamptz NOT NULL,
            used_at timestamptz,
            replaced_by_id uuid,
            CONSTRAINT uq_refresh_tokens_digest UNIQUE(digest),
            CONSTRAINT uq_refresh_tokens_family_id_id UNIQUE(family_id, id),
            CONSTRAINT fk_refresh_tokens_replacement FOREIGN KEY (family_id, replaced_by_id)
                REFERENCES refresh_tokens(family_id, id) DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_refresh_token_digest CHECK (octet_length(digest) = 32),
            CONSTRAINT ck_refresh_token_used CHECK (
                (used_at IS NULL AND replaced_by_id IS NULL) OR
                (used_at IS NOT NULL AND used_at >= issued_at
                 AND replaced_by_id IS NOT NULL AND replaced_by_id <> id)
            )
        );
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("refresh_families")
