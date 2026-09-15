"""Enable Dimension deletion and restoration with atomic field audit logs.

Revision ID: 20260915_0011
Revises: 20260911_0010
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0011"
down_revision: str | None = "20260911_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSIONS = ("company", "model", "brand", "country", "category", "year", "network", "memory")


def upgrade() -> None:
    for singular in _DIMENSIONS:
        _replace_triggers(singular, lifecycle=True)


def downgrade() -> None:
    for singular in _DIMENSIONS:
        _replace_triggers(singular, lifecycle=False)


def _replace_triggers(singular: str, *, lifecycle: bool) -> None:
    memory = singular == "memory"
    value_changed = (
        "proposed_capacity_mb IS DISTINCT FROM OLD.capacity_mb"
        if memory
        else "NEW.value IS DISTINCT FROM OLD.value"
    )
    raw_value_changed = (
        "NEW.amount IS DISTINCT FROM OLD.amount OR NEW.unit IS DISTINCT FROM OLD.unit"
        if memory
        else "NEW.value IS DISTINCT FROM OLD.value"
    )
    memory_preparation = (
        """
        proposed_capacity_mb := NEW.amount::BIGINT * CASE NEW.unit
            WHEN 'MB' THEN 1 WHEN 'GB' THEN 1000 WHEN 'TB' THEN 1000000 WHEN 'PB' THEN 1000000000
        END;
        IF proposed_capacity_mb IS NOT DISTINCT FROM OLD.capacity_mb
            AND (NEW.amount IS DISTINCT FROM OLD.amount OR NEW.unit IS DISTINCT FROM OLD.unit)
        THEN
            RAISE EXCEPTION 'equivalent memory value must preserve its stored representation'
                USING ERRCODE = '23514';
        END IF;
    """
        if memory
        else ""
    )
    lifecycle_guard = (
        f"""
        IF audit_context.dimension_operation IN ('DELETE', 'RESTORE') THEN
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.code IS DISTINCT FROM OLD.code OR ({raw_value_changed})
            THEN
                RAISE EXCEPTION '{singular} lifecycle changed a protected field'
                    USING ERRCODE = '23514';
            END IF;
            IF audit_context.dimension_operation = 'DELETE' THEN
                IF OLD.deleted_at IS NOT NULL
                    OR NEW.deleted_at IS DISTINCT FROM audit_context.mutation_timestamp
                THEN
                    RAISE EXCEPTION '{singular} invalid deletion' USING ERRCODE = '23514';
                END IF;
                IF EXISTS (SELECT 1 FROM master_codes
                           WHERE {singular}_id = OLD.id AND deleted_at IS NULL) THEN
                    RAISE EXCEPTION '{singular} has active references' USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.deleted_at IS NULL OR NEW.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION '{singular} invalid restoration' USING ERRCODE = '23514';
            END IF;
            IF NEW.version <> OLD.version + 1
                OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                OR NEW.updated_at < OLD.updated_at
            THEN
                RAISE EXCEPTION '{singular} lifecycle has invalid technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION '{singular} deleted row cannot be updated' USING ERRCODE = '23514';
        END IF;
    """
        if lifecycle
        else ""
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_before_write()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            audit_context RECORD;
            proposed_capacity_mb BIGINT;
            code_changed BOOLEAN;
            value_changed BOOLEAN;
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
            {lifecycle_guard}
            IF audit_context.dimension_operation IS DISTINCT FROM 'UPDATE' THEN
                RAISE EXCEPTION '{singular} update requires UPDATE context' USING ERRCODE = '23514';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
            THEN
                RAISE EXCEPTION '{singular} update changed a protected field'
                    USING ERRCODE = '23514';
            END IF;
            {memory_preparation}
            code_changed := NEW.code IS DISTINCT FROM OLD.code;
            value_changed := {value_changed};
            IF code_changed
                AND audit_context.master_code_operation IS DISTINCT FROM 'RECOMPOSE' THEN
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
                RAISE EXCEPTION '{singular} no-op changed technical fields' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$;
    """)
    lifecycle_log = (
        f"""
        IF (NEW.deleted_at IS NULL) IS DISTINCT FROM (OLD.deleted_at IS NULL) THEN
            INSERT INTO dimension_{singular}_logs (
                dimension_id, change_set_id, dimension_version, operation, field_name,
                old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
            ) VALUES (
                NEW.id, audit_context.change_set_id, NEW.version,
                CASE WHEN NEW.deleted_at IS NULL THEN 'RESTORE' ELSE 'DELETE' END, 'DELETED',
                to_jsonb(OLD.deleted_at IS NOT NULL), to_jsonb(NEW.deleted_at IS NOT NULL),
                audit_context.reason, audit_context.actor_kind, audit_context.actor_role,
                audit_context.actor_id, NEW.updated_at
            );
        END IF;
    """
        if lifecycle
        else ""
    )
    old_value = (
        "jsonb_build_object('amount', OLD.amount, 'unit', OLD.unit, 'capacity_mb', OLD.capacity_mb)"
        if memory
        else "to_jsonb(OLD.value)"
    )
    new_value = old_value.replace("OLD.", "NEW.")
    after_value_changed = (
        "NEW.capacity_mb IS DISTINCT FROM OLD.capacity_mb"
        if memory
        else "NEW.value IS DISTINCT FROM OLD.value"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION mdm_dimension_{singular}_after_update()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE audit_context RECORD;
        BEGIN
            SELECT * INTO audit_context FROM mdm_current_mutation_audit_context();
            {lifecycle_log}
            IF NEW.code IS DISTINCT FROM OLD.code THEN
                INSERT INTO dimension_{singular}_logs (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'CODE',
                    to_jsonb(OLD.code), to_jsonb(NEW.code), audit_context.reason,
                    audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                    NEW.updated_at
                );
            END IF;
            IF {after_value_changed} THEN
                INSERT INTO dimension_{singular}_logs (
                    dimension_id, change_set_id, dimension_version, operation, field_name,
                    old_value, new_value, reason, actor_kind, actor_role, actor_id, changed_at
                ) VALUES (
                    NEW.id, audit_context.change_set_id, NEW.version, 'UPDATE', 'VALUE',
                    {old_value}, {new_value}, audit_context.reason,
                    audit_context.actor_kind, audit_context.actor_role, audit_context.actor_id,
                    NEW.updated_at
                );
            END IF;
            RETURN NEW;
        END
        $function$;
    """)
