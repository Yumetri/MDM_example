"""Create the Company Dimension vertical slice.

Revision ID: 20260909_0003
Revises: 20260909_0002
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_0003"
down_revision: str | None = "20260909_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dimension_companies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=128), nullable=False),
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
        sa.CheckConstraint("code ~ '^[A-Z0-9]{1,32}$'", name="ck_dimension_companies_code"),
        sa.CheckConstraint("code !~ '^N+$'", name="ck_dimension_companies_code_reserved"),
        sa.CheckConstraint(
            "value ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' AND char_length(value) <= 128",
            name="ck_dimension_companies_value",
        ),
        sa.CheckConstraint("version >= 1", name="ck_dimension_companies_version"),
        sa.CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name="ck_dimension_companies_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_dimension_companies_code"),
        sa.UniqueConstraint("value", name="uq_dimension_companies_value"),
    )
    op.create_index(
        "ix_dimension_companies_active_created_id",
        "dimension_companies",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "dimension_company_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("dimension_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension_version", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=7), nullable=False),
        sa.Column("field_name", sa.String(length=7), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("actor_kind", sa.String(length=6), nullable=False),
        sa.Column("actor_role", sa.String(length=11), nullable=True),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("dimension_version >= 1", name="ck_dimension_company_logs_version"),
        sa.CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')",
            name="ck_dimension_company_logs_operation",
        ),
        sa.CheckConstraint(
            "field_name IN ('CODE', 'VALUE', 'DELETED')",
            name="ck_dimension_company_logs_field",
        ),
        sa.CheckConstraint(
            "((operation = 'CREATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NULL AND new_value IS NOT NULL) OR "
            "(operation = 'UPDATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value <> new_value) OR "
            "(operation = 'DELETE' AND field_name = 'DELETED' "
            "AND old_value = 'false'::jsonb AND new_value = 'true'::jsonb) OR "
            "(operation = 'RESTORE' AND field_name = 'DELETED' "
            "AND old_value = 'true'::jsonb AND new_value = 'false'::jsonb)) IS TRUE",
            name="ck_dimension_company_logs_change_shape",
        ),
        sa.CheckConstraint(
            "((field_name = 'CODE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (old_value #>> '{}') !~ '^N+$')) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (new_value #>> '{}') !~ '^N+$'))) OR "
            "(field_name = 'VALUE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(old_value #>> '{}') <= 128)) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' "
            "AND char_length(new_value #>> '{}') <= 128))) OR "
            "(field_name = 'DELETED' AND "
            "jsonb_typeof(old_value) = 'boolean' AND jsonb_typeof(new_value) = 'boolean'))",
            name="ck_dimension_company_logs_value_shape",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name="ck_dimension_company_logs_reason",
        ),
        sa.CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name="ck_dimension_company_logs_actor",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name="ck_dimension_company_logs_actor_id",
        ),
        sa.ForeignKeyConstraint(
            ["dimension_id"],
            ["dimension_companies.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dimension_id",
            "change_set_id",
            "field_name",
            name="uq_dimension_company_logs_change_field",
        ),
    )
    op.create_index(
        "ix_dimension_company_logs_dimension_changed_id",
        "dimension_company_logs",
        ["dimension_id", "changed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_dimension_company_logs_change_dimension_id",
        "dimension_company_logs",
        ["change_set_id", "dimension_id", "id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION mdm_dimension_company_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.dimension_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION 'company insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1
                    OR NEW.created_at <> NEW.updated_at
                    OR NEW.deleted_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'company insert has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'company update is not implemented'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_dimension_company_reject_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'physical company deletion is forbidden'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_dimension_company_after_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO dimension_company_logs (
                dimension_id, change_set_id, dimension_version, operation, field_name,
                old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
            )
            VALUES
                (NEW.id, audit_context.change_set_id, NEW.version, 'CREATE', 'CODE',
                 NULL, to_jsonb(NEW.code), audit_context.reason, audit_context.actor_kind,
                 audit_context.actor_role, audit_context.actor_id, NEW.updated_at),
                (NEW.id, audit_context.change_set_id, NEW.version, 'CREATE', 'VALUE',
                 NULL, to_jsonb(NEW.value), audit_context.reason, audit_context.actor_kind,
                 audit_context.actor_role, audit_context.actor_id, NEW.updated_at);
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_dimension_company_log_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'company audit logs are append-only'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER dimension_company_before_write
        BEFORE INSERT OR UPDATE ON dimension_companies
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_company_before_write()
        """
    )
    op.execute(
        """
        CREATE TRIGGER dimension_company_reject_delete
        BEFORE DELETE ON dimension_companies
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_company_reject_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER dimension_company_after_insert
        AFTER INSERT ON dimension_companies
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_company_after_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER dimension_company_log_reject_mutation
        BEFORE UPDATE OR DELETE ON dimension_company_logs
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_company_log_reject_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER dimension_company_log_reject_mutation ON dimension_company_logs")
    op.execute("DROP TRIGGER dimension_company_after_insert ON dimension_companies")
    op.execute("DROP TRIGGER dimension_company_reject_delete ON dimension_companies")
    op.execute("DROP TRIGGER dimension_company_before_write ON dimension_companies")
    op.execute("DROP FUNCTION mdm_dimension_company_log_reject_mutation()")
    op.execute("DROP FUNCTION mdm_dimension_company_after_insert()")
    op.execute("DROP FUNCTION mdm_dimension_company_reject_delete()")
    op.execute("DROP FUNCTION mdm_dimension_company_before_write()")
    op.drop_index(
        "ix_dimension_company_logs_change_dimension_id", table_name="dimension_company_logs"
    )
    op.drop_index(
        "ix_dimension_company_logs_dimension_changed_id", table_name="dimension_company_logs"
    )
    op.drop_table("dimension_company_logs")
    op.drop_index("ix_dimension_companies_active_created_id", table_name="dimension_companies")
    op.drop_table("dimension_companies")
