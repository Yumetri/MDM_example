"""Create Year and Network Dimension slices.

Revision ID: 20260910_0005
Revises: 20260910_0004
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0005"
down_revision: str | None = "20260910_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSIONS = (
    ("year", "years", 2000, 2999),
    ("network", "networks", 1, 5),
)


def upgrade() -> None:
    for singular, plural, minimum, maximum in _DIMENSIONS:
        _create_dimension_table(plural, minimum, maximum)
        _create_log_table(singular, plural, minimum, maximum)
        _create_triggers(singular, plural)


def downgrade() -> None:
    for singular, plural, _minimum, _maximum in reversed(_DIMENSIONS):
        op.execute(
            f"DROP TRIGGER dimension_{singular}_log_reject_mutation ON dimension_{singular}_logs"
        )
        op.execute(f"DROP TRIGGER dimension_{singular}_after_insert ON dimension_{plural}")
        op.execute(f"DROP TRIGGER dimension_{singular}_reject_delete ON dimension_{plural}")
        op.execute(f"DROP TRIGGER dimension_{singular}_before_write ON dimension_{plural}")
        op.execute(f"DROP FUNCTION mdm_dimension_{singular}_log_reject_mutation()")
        op.execute(f"DROP FUNCTION mdm_dimension_{singular}_after_insert()")
        op.execute(f"DROP FUNCTION mdm_dimension_{singular}_reject_delete()")
        op.execute(f"DROP FUNCTION mdm_dimension_{singular}_before_write()")
        op.drop_index(
            f"ix_dimension_{singular}_logs_change_dimension_id",
            table_name=f"dimension_{singular}_logs",
        )
        op.drop_index(
            f"ix_dimension_{singular}_logs_dimension_changed_id",
            table_name=f"dimension_{singular}_logs",
        )
        op.drop_table(f"dimension_{singular}_logs")
        op.drop_index(f"ix_dimension_{plural}_active_created_id", table_name=f"dimension_{plural}")
        op.drop_table(f"dimension_{plural}")


def _create_dimension_table(plural: str, minimum: int, maximum: int) -> None:
    table = f"dimension_{plural}"
    op.create_table(
        table,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuidv7()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
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
        sa.CheckConstraint("code ~ '^[A-Z0-9]{1,32}$'", name=f"ck_{table}_code"),
        sa.CheckConstraint("code !~ '^N+$'", name=f"ck_{table}_code_reserved"),
        sa.CheckConstraint(f"value BETWEEN {minimum} AND {maximum}", name=f"ck_{table}_value"),
        sa.CheckConstraint("version >= 1", name=f"ck_{table}_version"),
        sa.CheckConstraint(
            "updated_at >= created_at AND "
            "(deleted_at IS NULL OR "
            "(deleted_at >= created_at AND deleted_at <= updated_at))",
            name=f"ck_{table}_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name=f"uq_{table}_code"),
        sa.UniqueConstraint("value", name=f"uq_{table}_value"),
    )
    op.create_index(
        f"ix_{table}_active_created_id",
        table,
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def _create_log_table(singular: str, plural: str, minimum: int, maximum: int) -> None:
    table = f"dimension_{singular}_logs"
    old_numeric = _numeric_json_constraint("old_value", minimum, maximum)
    new_numeric = _numeric_json_constraint("new_value", minimum, maximum)
    op.create_table(
        table,
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
        sa.CheckConstraint("dimension_version >= 1", name=f"ck_{table}_version"),
        sa.CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')",
            name=f"ck_{table}_operation",
        ),
        sa.CheckConstraint("field_name IN ('CODE', 'VALUE', 'DELETED')", name=f"ck_{table}_field"),
        sa.CheckConstraint(
            "((operation = 'CREATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NULL AND new_value IS NOT NULL) OR "
            "(operation = 'UPDATE' AND field_name IN ('CODE', 'VALUE') "
            "AND old_value IS NOT NULL AND new_value IS NOT NULL AND old_value <> new_value) OR "
            "(operation = 'DELETE' AND field_name = 'DELETED' "
            "AND old_value = 'false'::jsonb AND new_value = 'true'::jsonb) OR "
            "(operation = 'RESTORE' AND field_name = 'DELETED' "
            "AND old_value = 'true'::jsonb AND new_value = 'false'::jsonb)) IS TRUE",
            name=f"ck_{table}_change_shape",
        ),
        sa.CheckConstraint(
            "((field_name = 'CODE' AND "
            "(old_value IS NULL OR (jsonb_typeof(old_value) = 'string' "
            "AND (old_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (old_value #>> '{}') !~ '^N+$')) AND "
            "(new_value IS NULL OR (jsonb_typeof(new_value) = 'string' "
            "AND (new_value #>> '{}') ~ '^[A-Z0-9]{1,32}$' "
            "AND (new_value #>> '{}') !~ '^N+$'))) OR "
            f"(field_name = 'VALUE' AND (old_value IS NULL OR ({old_numeric})) "
            f"AND (new_value IS NULL OR ({new_numeric}))) OR "
            "(field_name = 'DELETED' AND "
            "jsonb_typeof(old_value) = 'boolean' AND jsonb_typeof(new_value) = 'boolean'))",
            name=f"ck_{table}_value_shape",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR (char_length(reason) <= 500 "
            "AND btrim(reason, ' ') = reason AND reason !~ '[[:cntrl:]]')",
            name=f"ck_{table}_reason",
        ),
        sa.CheckConstraint(
            "((actor_kind = 'HUMAN' AND actor_role IN ('USER', 'ADMIN', 'SUPER_ADMIN')) "
            "OR (actor_kind = 'SYSTEM' AND actor_role IS NULL)) IS TRUE",
            name=f"ck_{table}_actor",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 255 AND btrim(actor_id, ' ') = actor_id",
            name=f"ck_{table}_actor_id",
        ),
        sa.ForeignKeyConstraint(
            ["dimension_id"],
            [f"dimension_{plural}.id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dimension_id",
            "change_set_id",
            "field_name",
            name=f"uq_{table}_change_field",
        ),
    )
    op.create_index(
        f"ix_{table}_dimension_changed_id",
        table,
        ["dimension_id", "changed_at", "id"],
        unique=False,
    )
    op.create_index(
        f"ix_{table}_change_dimension_id",
        table,
        ["change_set_id", "dimension_id", "id"],
        unique=False,
    )


def _numeric_json_constraint(value: str, minimum: int, maximum: int) -> str:
    return (
        f"jsonb_typeof({value}) = 'number' "
        f"AND ({value} #>> '{{}}') ~ '^[0-9]+$' "
        f"AND ({value} #>> '{{}}')::integer BETWEEN {minimum} AND {maximum}"
    )


def _create_triggers(singular: str, plural: str) -> None:
    dimension_table = f"dimension_{plural}"
    log_table = f"dimension_{singular}_logs"
    op.execute(
        f"""
        CREATE FUNCTION mdm_dimension_{singular}_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.dimension_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION '{singular} insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1
                    OR NEW.created_at <> NEW.updated_at
                    OR NEW.deleted_at IS NOT NULL
                THEN
                    RAISE EXCEPTION '{singular} insert has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION '{singular} update is not implemented'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION mdm_dimension_{singular}_reject_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'physical {singular} deletion is forbidden'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION mdm_dimension_{singular}_after_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO {log_table} (
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
        f"""
        CREATE FUNCTION mdm_dimension_{singular}_log_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION '{singular} audit logs are append-only'
                USING ERRCODE = '23514';
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER dimension_{singular}_before_write
        BEFORE INSERT OR UPDATE ON {dimension_table}
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_{singular}_before_write()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER dimension_{singular}_reject_delete
        BEFORE DELETE ON {dimension_table}
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_{singular}_reject_delete()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER dimension_{singular}_after_insert
        AFTER INSERT ON {dimension_table}
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_{singular}_after_insert()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER dimension_{singular}_log_reject_mutation
        BEFORE UPDATE OR DELETE ON {log_table}
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_{singular}_log_reject_mutation()
        """
    )
