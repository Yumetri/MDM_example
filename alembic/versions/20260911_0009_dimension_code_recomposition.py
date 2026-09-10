"""Enable Dimension code changes and synchronous MasterCode recomposition.

Revision ID: 20260911_0009
Revises: 20260910_0008
Create Date: 2026-09-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260911_0009"
down_revision: str | None = "20260910_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCALAR_DIMENSIONS = (
    ("company", "companies", "dimension_company_logs"),
    ("model", "models", "dimension_model_logs"),
    ("brand", "brands", "dimension_brand_logs"),
    ("country", "countries", "dimension_country_logs"),
    ("category", "categories", "dimension_category_logs"),
    ("year", "years", "dimension_year_logs"),
    ("network", "networks", "dimension_network_logs"),
)


def upgrade() -> None:
    for singular, plural, log_table in _SCALAR_DIMENSIONS:
        _enable_scalar_code_update(singular, plural, log_table)
    _enable_memory_code_update()
    _enable_master_code_recomposition()


def downgrade() -> None:
    op.execute("DROP TRIGGER master_code_after_update ON master_codes")
    op.execute("DROP FUNCTION mdm_master_code_after_update()")
    _restore_master_code_create_only_guard()
    for singular, plural, log_table in _SCALAR_DIMENSIONS:
        _restore_scalar_value_only_update(singular, plural, log_table)
    _restore_memory_value_only_update()


def _enable_scalar_code_update(singular: str, plural: str, log_table: str) -> None:
    del plural
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
            code_changed BOOLEAN;
            value_changed BOOLEAN;
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
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION '{singular} update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;

            code_changed := NEW.code IS DISTINCT FROM OLD.code;
            value_changed := NEW.value IS DISTINCT FROM OLD.value;
            IF code_changed
                AND audit_context.master_code_operation IS DISTINCT FROM 'RECOMPOSE'
            THEN
                RAISE EXCEPTION '{singular} code update requires RECOMPOSE context'
                    USING ERRCODE = '23514';
            END IF;
            IF code_changed OR value_changed THEN
                IF NEW.version <> OLD.version + 1
                    OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                    OR NEW.updated_at < OLD.updated_at
                THEN
                    RAISE EXCEPTION '{singular} update has invalid technical fields'
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
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF NEW.code IS DISTINCT FROM OLD.code THEN
                INSERT INTO {log_table} (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'CODE',
                    to_jsonb(OLD.code), to_jsonb(NEW.code), audit_context.reason,
                    audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                    NEW.updated_at
                );
            END IF;
            IF NEW.value IS DISTINCT FROM OLD.value THEN
                INSERT INTO {log_table} (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                    to_jsonb(OLD.value), to_jsonb(NEW.value), audit_context.reason,
                    audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                    NEW.updated_at
                );
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )


def _enable_memory_code_update() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_dimension_memory_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
            proposed_capacity_mb BIGINT;
            code_changed BOOLEAN;
            value_changed BOOLEAN;
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
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION 'memory update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;

            proposed_capacity_mb := NEW.amount::BIGINT * CASE NEW.unit
                WHEN 'MB' THEN 1
                WHEN 'GB' THEN 1000
                WHEN 'TB' THEN 1000000
                WHEN 'PB' THEN 1000000000
            END;
            IF proposed_capacity_mb IS NOT DISTINCT FROM OLD.capacity_mb
                AND (NEW.amount IS DISTINCT FROM OLD.amount OR NEW.unit IS DISTINCT FROM OLD.unit)
            THEN
                RAISE EXCEPTION 'equivalent memory value must preserve its stored representation'
                    USING ERRCODE = '23514';
            END IF;

            code_changed := NEW.code IS DISTINCT FROM OLD.code;
            value_changed := proposed_capacity_mb IS DISTINCT FROM OLD.capacity_mb;
            IF code_changed
                AND audit_context.master_code_operation IS DISTINCT FROM 'RECOMPOSE'
            THEN
                RAISE EXCEPTION 'memory code update requires RECOMPOSE context'
                    USING ERRCODE = '23514';
            END IF;
            IF code_changed OR value_changed THEN
                IF NEW.version <> OLD.version + 1
                    OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                    OR NEW.updated_at < OLD.updated_at
                THEN
                    RAISE EXCEPTION 'memory update has invalid technical fields'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.version IS DISTINCT FROM OLD.version
                OR NEW.updated_at IS DISTINCT FROM OLD.updated_at
            THEN
                RAISE EXCEPTION 'memory no-op changed technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_dimension_memory_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF NEW.code IS DISTINCT FROM OLD.code THEN
                INSERT INTO dimension_memory_logs (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'CODE',
                    to_jsonb(OLD.code), to_jsonb(NEW.code), audit_context.reason,
                    audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                    NEW.updated_at
                );
            END IF;
            IF NEW.capacity_mb IS DISTINCT FROM OLD.capacity_mb THEN
                INSERT INTO dimension_memory_logs (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                    jsonb_build_object(
                        'amount', OLD.amount, 'unit', OLD.unit, 'capacity_mb', OLD.capacity_mb
                    ),
                    jsonb_build_object(
                        'amount', NEW.amount, 'unit', NEW.unit, 'capacity_mb', NEW.capacity_mb
                    ),
                    audit_context.reason, audit_context.actor_kind, audit_context.actor_role,
                    audit_context.actor_id, NEW.updated_at
                );
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )


def _enable_master_code_recomposition() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_master_code_before_write()
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

            IF audit_context.master_code_operation IS DISTINCT FROM 'RECOMPOSE' THEN
                RAISE EXCEPTION 'master code update operation is not implemented'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.brand_id IS DISTINCT FROM OLD.brand_id
                OR NEW.model_id IS DISTINCT FROM OLD.model_id
                OR NEW.category_id IS DISTINCT FROM OLD.category_id
                OR NEW.year_id IS DISTINCT FROM OLD.year_id
                OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
                OR NEW.network_id IS DISTINCT FROM OLD.network_id
                OR NEW.country_id IS DISTINCT FROM OLD.country_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
                OR NEW.code IS NOT DISTINCT FROM OLD.code
            THEN
                RAISE EXCEPTION 'master code recomposition has an invalid state transition'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.version <> OLD.version + 1
                OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                OR NEW.updated_at < OLD.updated_at
            THEN
                RAISE EXCEPTION 'master code recomposition has invalid technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION mdm_master_code_after_update()
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
                NEW.id, audit_context.change_set_id, NEW.version, 'RECOMPOSE',
                jsonb_build_object(
                    'company_id', OLD.company_id, 'brand_id', OLD.brand_id,
                    'model_id', OLD.model_id, 'category_id', OLD.category_id,
                    'year_id', OLD.year_id, 'memory_id', OLD.memory_id,
                    'network_id', OLD.network_id, 'country_id', OLD.country_id,
                    'code', OLD.code, 'deleted', OLD.deleted_at IS NOT NULL
                ),
                jsonb_build_object(
                    'company_id', NEW.company_id, 'brand_id', NEW.brand_id,
                    'model_id', NEW.model_id, 'category_id', NEW.category_id,
                    'year_id', NEW.year_id, 'memory_id', NEW.memory_id,
                    'network_id', NEW.network_id, 'country_id', NEW.country_id,
                    'code', NEW.code, 'deleted', NEW.deleted_at IS NOT NULL
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
        CREATE TRIGGER master_code_after_update
        AFTER UPDATE ON master_codes
        FOR EACH ROW EXECUTE FUNCTION mdm_master_code_after_update()
        """
    )


def _restore_scalar_value_only_update(singular: str, plural: str, log_table: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.dimension_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION '{singular} insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1 OR NEW.created_at <> NEW.updated_at
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
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.code IS DISTINCT FROM OLD.code
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
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE audit_context RECORD;
        BEGIN
            IF NEW.value IS NOT DISTINCT FROM OLD.value THEN RETURN NEW; END IF;
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


def _restore_memory_value_only_update() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_dimension_memory_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE audit_context RECORD; proposed_capacity_mb BIGINT;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.dimension_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION 'memory insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1 OR NEW.created_at <> NEW.updated_at
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
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.code IS DISTINCT FROM OLD.code
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION 'memory value update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;
            proposed_capacity_mb := NEW.amount::BIGINT * CASE NEW.unit
                WHEN 'MB' THEN 1 WHEN 'GB' THEN 1000 WHEN 'TB' THEN 1000000
                WHEN 'PB' THEN 1000000000 END;
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
        CREATE OR REPLACE FUNCTION mdm_dimension_memory_after_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE audit_context RECORD;
        BEGIN
            IF NEW.capacity_mb IS NOT DISTINCT FROM OLD.capacity_mb THEN RETURN NEW; END IF;
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            INSERT INTO dimension_memory_logs (
                dimension_id, change_set_id, dimension_version, operation, field_name,
                old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
            ) VALUES (
                NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                jsonb_build_object('amount', OLD.amount, 'unit', OLD.unit,
                    'capacity_mb', OLD.capacity_mb),
                jsonb_build_object('amount', NEW.amount, 'unit', NEW.unit,
                    'capacity_mb', NEW.capacity_mb),
                audit_context.reason, audit_context.actor_kind, audit_context.actor_role,
                audit_context.actor_id, NEW.updated_at
            );
            RETURN NEW;
        END
        $function$
        """
    )


def _restore_master_code_create_only_guard() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_master_code_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            IF TG_OP = 'INSERT' THEN
                IF audit_context.master_code_operation IS DISTINCT FROM 'CREATE' THEN
                    RAISE EXCEPTION 'master code insert requires CREATE context'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.version <> 1 OR NEW.created_at <> NEW.updated_at
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
