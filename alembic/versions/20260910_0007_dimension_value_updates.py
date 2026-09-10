"""Enable conditional Dimension value updates and audit logs.

Revision ID: 20260910_0007
Revises: 20260910_0006
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260910_0007"
down_revision: str | None = "20260910_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALUE_DIMENSIONS = (
    ("company", "companies", "dimension_company_logs"),
    ("model", "models", "dimension_model_logs"),
    ("brand", "brands", "dimension_brand_logs"),
    ("country", "countries", "dimension_country_logs"),
    ("category", "categories", "dimension_category_logs"),
    ("year", "years", "dimension_year_logs"),
    ("network", "networks", "dimension_network_logs"),
)


def upgrade() -> None:
    for singular, plural, log_table in _VALUE_DIMENSIONS:
        _enable_scalar_value_update(singular, plural, log_table)
    _enable_memory_value_update()


def downgrade() -> None:
    for singular, plural, _log_table in _VALUE_DIMENSIONS:
        op.execute(f"DROP TRIGGER dimension_{singular}_after_update ON dimension_{plural}")
        op.execute(f"DROP FUNCTION mdm_dimension_{singular}_after_update()")
        _restore_create_only_guard(singular)
    op.execute("DROP TRIGGER dimension_memory_after_update ON dimension_memories")
    op.execute("DROP FUNCTION mdm_dimension_memory_after_update()")
    _restore_create_only_guard("memory")


def _enable_scalar_value_update(singular: str, plural: str, log_table: str) -> None:
    table = f"dimension_{plural}"
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_before_write()
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

            IF audit_context.dimension_operation IS DISTINCT FROM 'UPDATE' THEN
                RAISE EXCEPTION '{singular} update requires UPDATE context'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.code IS DISTINCT FROM OLD.code
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION '{singular} value update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.value IS DISTINCT FROM OLD.value THEN
                IF NEW.version <> OLD.version + 1
                    OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                    OR NEW.updated_at < OLD.updated_at
                THEN
                    RAISE EXCEPTION '{singular} value update has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.version IS DISTINCT FROM OLD.version
                OR NEW.updated_at IS DISTINCT FROM OLD.updated_at
            THEN
                RAISE EXCEPTION '{singular} no-op changed technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION mdm_dimension_{singular}_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            IF NEW.value IS NOT DISTINCT FROM OLD.value THEN
                RETURN NEW;
            END IF;
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO {log_table} (
                dimension_id, change_set_id, dimension_version, operation, field_name,
                old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
            ) VALUES (
                NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                to_jsonb(OLD.value), to_jsonb(NEW.value), audit_context.reason,
                audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                NEW.updated_at
            );
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER dimension_{singular}_after_update
        AFTER UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_{singular}_after_update()
        """
    )


def _enable_memory_value_update() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_dimension_memory_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
            proposed_capacity_mb BIGINT;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.dimension_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION 'memory insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1
                    OR NEW.created_at <> NEW.updated_at
                    OR NEW.deleted_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'memory insert has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF audit_context.dimension_operation IS DISTINCT FROM 'UPDATE' THEN
                RAISE EXCEPTION 'memory update requires UPDATE context'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.code IS DISTINCT FROM OLD.code
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION 'memory value update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;

            proposed_capacity_mb := NEW.amount::BIGINT * CASE NEW.unit
                WHEN 'MB' THEN 1
                WHEN 'GB' THEN 1000
                WHEN 'TB' THEN 1000000
                WHEN 'PB' THEN 1000000000
            END;
            IF proposed_capacity_mb IS NOT DISTINCT FROM OLD.capacity_mb THEN
                IF NEW.amount IS DISTINCT FROM OLD.amount OR NEW.unit IS DISTINCT FROM OLD.unit THEN
                    RAISE EXCEPTION
                        'equivalent memory value must preserve its stored representation'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version IS DISTINCT FROM OLD.version
                    OR NEW.updated_at IS DISTINCT FROM OLD.updated_at
                THEN
                    RAISE EXCEPTION 'memory no-op changed technical fields'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.version <> OLD.version + 1
                OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                OR NEW.updated_at < OLD.updated_at
            THEN
                RAISE EXCEPTION 'memory value update has invalid technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_dimension_memory_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            IF NEW.capacity_mb IS NOT DISTINCT FROM OLD.capacity_mb THEN
                RETURN NEW;
            END IF;
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO dimension_memory_logs (
                dimension_id, change_set_id, dimension_version, operation, field_name,
                old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
            ) VALUES (
                NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                jsonb_build_object(
                    'amount', OLD.amount,
                    'unit', OLD.unit,
                    'capacity_mb', OLD.capacity_mb
                ),
                jsonb_build_object(
                    'amount', NEW.amount,
                    'unit', NEW.unit,
                    'capacity_mb', NEW.capacity_mb
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
        CREATE TRIGGER dimension_memory_after_update
        AFTER UPDATE ON dimension_memories
        FOR EACH ROW EXECUTE FUNCTION mdm_dimension_memory_after_update()
        """
    )


def _restore_create_only_guard(singular: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_before_write()
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
