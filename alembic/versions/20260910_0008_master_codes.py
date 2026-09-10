"""Create MasterCode create/read persistence and audit tables.

Revision ID: 20260910_0008
Revises: 20260910_0007
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0008"
down_revision: str | None = "20260910_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCES = (
    ("company_id", "dimension_companies"),
    ("brand_id", "dimension_brands"),
    ("model_id", "dimension_models"),
    ("category_id", "dimension_categories"),
    ("year_id", "dimension_years"),
    ("memory_id", "dimension_memories"),
    ("network_id", "dimension_networks"),
    ("country_id", "dimension_countries"),
)

_REFERENCE_COLUMNS = tuple(column for column, _ in _REFERENCES)
_STATE_KEYS = (*_REFERENCE_COLUMNS, "code", "deleted")


def _state_json_constraint(column: str) -> str:
    keys = ", ".join(f"'{key}'" for key in _STATE_KEYS)
    reference_checks = " AND ".join(
        f"(jsonb_typeof({column}->'{key}') IN ('string', 'null') AND "
        f"(jsonb_typeof({column}->'{key}') = 'null' OR "
        f"({column}->>'{key}') ~ "
        "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))"
        for key in _REFERENCE_COLUMNS
    )
    return (
        f"jsonb_typeof({column}) = 'object' "
        f"AND {column} ?& ARRAY[{keys}] "
        f"AND {column} - ARRAY[{keys}] = '{{}}'::jsonb "
        f"AND {reference_checks} "
        f"AND jsonb_typeof({column}->'code') = 'string' "
        f"AND ({column}->>'code') ~ '^[A-Z0-9]{{1,32}}(-[A-Z0-9]{{1,32}}){{7}}$' "
        f"AND jsonb_typeof({column}->'deleted') = 'boolean'"
    )


def upgrade() -> None:
    op.create_table(
        "master_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        *(
            sa.Column(
                column,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT", onupdate="RESTRICT"),
                nullable=True,
            )
            for column, table in _REFERENCES
        ),
        sa.Column("code", sa.String(length=263), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("statement_timestamp()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "code ~ '^[A-Z0-9]{1,32}(-[A-Z0-9]{1,32}){7}$'",
            name="ck_master_codes_code",
        ),
        sa.CheckConstraint("version >= 1", name="ck_master_codes_version"),
        sa.CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name="ck_master_codes_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_master_codes_code"),
        sa.UniqueConstraint(
            *(column for column, _ in _REFERENCES),
            name="uq_master_codes_references",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_master_codes_active_created_id",
        "master_codes",
        ["created_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    for column, _ in _REFERENCES:
        op.create_index(f"ix_master_codes_{column}", "master_codes", [column])

    op.create_table(
        "master_code_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("master_code_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("master_code_version", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("old_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("actor_kind", sa.String(length=6), nullable=False),
        sa.Column("actor_role", sa.String(length=11), nullable=True),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("master_code_version >= 1", name="ck_master_code_logs_version"),
        sa.CheckConstraint(
            "operation IN ('CREATE', 'REFERENCE_UPDATE', 'RECOMPOSE', 'DELETE', 'RESTORE')",
            name="ck_master_code_logs_operation",
        ),
        sa.CheckConstraint(
            "((operation = 'CREATE' AND old_state IS NULL) OR "
            "(operation <> 'CREATE' AND old_state IS NOT NULL)) IS TRUE",
            name="ck_master_code_logs_old_state",
        ),
        sa.CheckConstraint(
            f"old_state IS NULL OR ({_state_json_constraint('old_state')})",
            name="ck_master_code_logs_old_state_shape",
        ),
        sa.CheckConstraint(
            _state_json_constraint("new_state"),
            name="ck_master_code_logs_new_state_shape",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name="ck_master_code_logs_reason",
        ),
        sa.CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name="ck_master_code_logs_actor",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name="ck_master_code_logs_actor_id",
        ),
        sa.ForeignKeyConstraint(
            ["master_code_id"],
            ["master_codes.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "master_code_id",
            "master_code_version",
            name="uq_master_code_logs_master_version",
        ),
    )
    op.create_index(
        "ix_master_code_logs_master_changed_id",
        "master_code_logs",
        ["master_code_id", "changed_at", "id"],
    )
    op.create_index(
        "ix_master_code_logs_change_master",
        "master_code_logs",
        ["change_set_id", "master_code_id"],
    )
    _create_triggers()


def downgrade() -> None:
    op.execute("DROP TRIGGER master_code_log_reject_mutation ON master_code_logs")
    op.execute("DROP TRIGGER master_code_after_insert ON master_codes")
    op.execute("DROP TRIGGER master_code_reject_delete ON master_codes")
    op.execute("DROP TRIGGER master_code_before_write ON master_codes")
    op.execute("DROP FUNCTION mdm_master_code_log_reject_mutation()")
    op.execute("DROP FUNCTION mdm_master_code_after_insert()")
    op.execute("DROP FUNCTION mdm_master_code_reject_delete()")
    op.execute("DROP FUNCTION mdm_master_code_before_write()")
    op.drop_index("ix_master_code_logs_change_master", table_name="master_code_logs")
    op.drop_index("ix_master_code_logs_master_changed_id", table_name="master_code_logs")
    op.drop_table("master_code_logs")
    for column, _ in reversed(_REFERENCES):
        op.drop_index(f"ix_master_codes_{column}", table_name="master_codes")
    op.drop_index("ix_master_codes_active_created_id", table_name="master_codes")
    op.drop_table("master_codes")


def _create_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION mdm_master_code_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.master_code_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION 'master code insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1
                    OR NEW.created_at <> NEW.updated_at
                    OR NEW.created_at <> audit_context.mutation_timestamp
                    OR NEW.deleted_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'master code insert has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'master code update is not implemented'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_master_code_reject_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'physical master code deletion is forbidden'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_master_code_after_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO master_code_logs (
                master_code_id, change_set_id, master_code_version, operation,
                old_state, new_state, reason, actor_kind, actor_role, actor_id, changed_at
            ) VALUES (
                NEW.id, audit_context.change_set_id, NEW.version, 'CREATE', NULL,
                jsonb_build_object(
                    'company_id', NEW.company_id, 'brand_id', NEW.brand_id,
                    'model_id', NEW.model_id, 'category_id', NEW.category_id,
                    'year_id', NEW.year_id, 'memory_id', NEW.memory_id,
                    'network_id', NEW.network_id, 'country_id', NEW.country_id,
                    'code', NEW.code, 'deleted', false
                ),
                audit_context.reason, audit_context.actor_kind, audit_context.actor_role,
                audit_context.actor_id, NEW.updated_at
            );
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_master_code_log_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'master code audit logs are append-only'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER master_code_before_write
        BEFORE INSERT OR UPDATE ON master_codes
        FOR EACH ROW EXECUTE FUNCTION mdm_master_code_before_write()
        """
    )
    op.execute(
        """
        CREATE TRIGGER master_code_reject_delete
        BEFORE DELETE ON master_codes
        FOR EACH ROW EXECUTE FUNCTION mdm_master_code_reject_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER master_code_after_insert
        AFTER INSERT ON master_codes
        FOR EACH ROW EXECUTE FUNCTION mdm_master_code_after_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER master_code_log_reject_mutation
        BEFORE UPDATE OR DELETE ON master_code_logs
        FOR EACH ROW EXECUTE FUNCTION mdm_master_code_log_reject_mutation()
        """
    )
