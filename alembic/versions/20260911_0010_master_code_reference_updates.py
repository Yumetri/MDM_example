"""Enable conditional MasterCode reference updates and audit logs.

Revision ID: 20260911_0010
Revises: 20260911_0009
Create Date: 2026-09-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260911_0010"
down_revision: str | None = "20260911_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _enable_reference_updates()


def downgrade() -> None:
    _restore_recomposition_only()


def _enable_reference_updates() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_master_code_before_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            audit_context RECORD;
            references_changed BOOLEAN;
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

            references_changed :=
                NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.brand_id IS DISTINCT FROM OLD.brand_id
                OR NEW.model_id IS DISTINCT FROM OLD.model_id
                OR NEW.category_id IS DISTINCT FROM OLD.category_id
                OR NEW.year_id IS DISTINCT FROM OLD.year_id
                OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
                OR NEW.network_id IS DISTINCT FROM OLD.network_id
                OR NEW.country_id IS DISTINCT FROM OLD.country_id;

            IF audit_context.master_code_operation = 'RECOMPOSE' THEN
                IF references_changed OR NEW.code IS NOT DISTINCT FROM OLD.code THEN
                    RAISE EXCEPTION 'master code recomposition has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF audit_context.master_code_operation = 'REFERENCE_UPDATE' THEN
                IF NOT references_changed OR NEW.code IS NOT DISTINCT FROM OLD.code THEN
                    RAISE EXCEPTION 'master code reference update has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'master code update operation is not implemented'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
                OR NEW.version <> OLD.version + 1
                OR NEW.updated_at IS DISTINCT FROM audit_context.mutation_timestamp
                OR NEW.updated_at < OLD.updated_at
            THEN
                RAISE EXCEPTION 'master code update has invalid technical fields'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mdm_master_code_after_update()
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
                NEW.id, audit_context.change_set_id, NEW.version,
                audit_context.master_code_operation,
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


def _restore_recomposition_only() -> None:
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
        CREATE OR REPLACE FUNCTION mdm_master_code_after_update()
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
