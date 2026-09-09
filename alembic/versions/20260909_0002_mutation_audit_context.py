"""Add reusable PostgreSQL mutation audit context validation.

Revision ID: 20260909_0002
Revises: 20260909_0001
Create Date: 2026-09-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260909_0002"
down_revision: str | None = "20260909_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION mdm_current_mutation_audit_context()
        RETURNS TABLE (
            change_set_id UUID,
            actor_kind TEXT,
            actor_role TEXT,
            actor_id TEXT,
            reason TEXT,
            dimension_operation TEXT,
            master_code_operation TEXT,
            mutation_timestamp TIMESTAMPTZ
        )
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            context_change_set_id TEXT := NULLIF(
                current_setting('mdm.change_set_id', true), ''
            );
            context_actor_kind TEXT := NULLIF(current_setting('mdm.actor_kind', true), '');
            context_actor_role TEXT := NULLIF(current_setting('mdm.actor_role', true), '');
            context_actor_id TEXT := NULLIF(current_setting('mdm.actor_id', true), '');
            context_reason TEXT := NULLIF(current_setting('mdm.reason', true), '');
            context_dimension_operation TEXT := NULLIF(
                current_setting('mdm.dimension_operation', true), ''
            );
            context_master_code_operation TEXT := NULLIF(
                current_setting('mdm.master_code_operation', true), ''
            );
            context_mutation_timestamp TEXT := NULLIF(
                current_setting('mdm.mutation_timestamp', true), ''
            );
            parsed_change_set_id UUID;
            parsed_mutation_timestamp TIMESTAMPTZ;
        BEGIN
            IF context_actor_kind IS NULL
                OR context_actor_kind NOT IN ('HUMAN', 'SYSTEM')
            THEN
                RAISE EXCEPTION 'mutation audit context has an invalid actor kind'
                    USING ERRCODE = '23514';
            END IF;

            IF context_actor_id IS NULL
                OR char_length(context_actor_id) > 255
                OR btrim(context_actor_id, ' ') <> context_actor_id
            THEN
                RAISE EXCEPTION 'mutation audit context has an invalid actor id'
                    USING ERRCODE = '23514';
            END IF;

            IF context_actor_kind = 'HUMAN' THEN
                IF context_actor_role IS NULL
                    OR context_actor_role NOT IN ('USER', 'ADMIN', 'SUPER_ADMIN')
                THEN
                    RAISE EXCEPTION 'mutation audit context has an invalid human role'
                        USING ERRCODE = '23514';
                END IF;
                BEGIN
                    IF context_actor_id::UUID::TEXT <> context_actor_id THEN
                        RAISE EXCEPTION 'mutation audit context has a non-canonical human id'
                            USING ERRCODE = '23514';
                    END IF;
                EXCEPTION
                    WHEN invalid_text_representation THEN
                        RAISE EXCEPTION 'mutation audit context has an invalid human id'
                            USING ERRCODE = '23514';
                END;
            ELSIF context_actor_role IS NOT NULL THEN
                RAISE EXCEPTION 'mutation audit context system actor has a role'
                    USING ERRCODE = '23514';
            END IF;

            IF context_change_set_id IS NULL THEN
                RAISE EXCEPTION 'mutation audit context has no change set id'
                    USING ERRCODE = '23514';
            END IF;
            BEGIN
                parsed_change_set_id := context_change_set_id::UUID;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION 'mutation audit context has an invalid change set id'
                        USING ERRCODE = '23514';
            END;
            IF substring(parsed_change_set_id::TEXT FROM 15 FOR 1) <> '7'
                OR substring(parsed_change_set_id::TEXT FROM 20 FOR 1)
                    NOT IN ('8', '9', 'a', 'b')
            THEN
                RAISE EXCEPTION 'mutation audit context change set id is not UUIDv7'
                    USING ERRCODE = '23514';
            END IF;

            IF context_reason IS NOT NULL AND (
                char_length(context_reason) > 500
                OR btrim(context_reason, ' ') <> context_reason
                OR context_reason ~ '[[:cntrl:]]'
            ) THEN
                RAISE EXCEPTION 'mutation audit context has an invalid reason'
                    USING ERRCODE = '23514';
            END IF;

            IF context_dimension_operation IS NOT NULL
                AND context_dimension_operation NOT IN ('CREATE', 'UPDATE', 'DELETE', 'RESTORE')
            THEN
                RAISE EXCEPTION 'mutation audit context has an invalid dimension operation'
                    USING ERRCODE = '23514';
            END IF;
            IF context_master_code_operation IS NOT NULL
                AND context_master_code_operation NOT IN (
                    'CREATE', 'REFERENCE_UPDATE', 'RECOMPOSE', 'DELETE', 'RESTORE'
                )
            THEN
                RAISE EXCEPTION 'mutation audit context has an invalid master code operation'
                    USING ERRCODE = '23514';
            END IF;
            IF context_dimension_operation IS NULL AND context_master_code_operation IS NULL THEN
                RAISE EXCEPTION 'mutation audit context has no entity operation'
                    USING ERRCODE = '23514';
            END IF;

            IF context_mutation_timestamp IS NULL THEN
                RAISE EXCEPTION 'mutation audit context has no mutation timestamp'
                    USING ERRCODE = '23514';
            END IF;
            BEGIN
                parsed_mutation_timestamp := context_mutation_timestamp::TIMESTAMPTZ;
            EXCEPTION
                WHEN invalid_datetime_format OR datetime_field_overflow THEN
                    RAISE EXCEPTION 'mutation audit context has an invalid mutation timestamp'
                        USING ERRCODE = '23514';
            END;

            RETURN QUERY SELECT
                parsed_change_set_id,
                context_actor_kind,
                context_actor_role,
                context_actor_id,
                context_reason,
                context_dimension_operation,
                context_master_code_operation,
                parsed_mutation_timestamp;
        END
        $function$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION mdm_current_mutation_audit_context()")
