"""Enable conditional MasterCode deletion and restoration audit transitions.

Revision ID: 20260915_0012
Revises: 20260915_0011
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0012"
down_revision: str | None = "20260915_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
                IF references_changed OR NEW.code IS NOT DISTINCT FROM OLD.code
                    OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
                    RAISE EXCEPTION 'master code recomposition has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF audit_context.master_code_operation = 'REFERENCE_UPDATE' THEN
                IF NOT references_changed OR NEW.code IS NOT DISTINCT FROM OLD.code
                    OR OLD.deleted_at IS NOT NULL OR NEW.deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'master code reference update has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF audit_context.master_code_operation = 'DELETE' THEN
                IF references_changed OR NEW.code IS DISTINCT FROM OLD.code
                    OR OLD.deleted_at IS NOT NULL
                    OR NEW.deleted_at IS DISTINCT FROM audit_context.mutation_timestamp
                THEN
                    RAISE EXCEPTION 'master code deletion has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF audit_context.master_code_operation = 'RESTORE' THEN
                IF references_changed OR OLD.deleted_at IS NULL OR NEW.deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'master code restoration has an invalid state transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'master code update operation is not implemented'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
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


def downgrade() -> None:
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
