"""Store immutable MasterCode proposals and single atomic review outcomes.

Revision ID: 20260915_0013
Revises: 20260915_0012
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0013"
down_revision: str | None = "20260915_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE master_code_change_requests (
            id UUID PRIMARY KEY DEFAULT uuidv7(),
            original_operation VARCHAR(16) NOT NULL,
            original_target_id UUID,
            original_payload JSONB,
            original_expected_etag TEXT,
            requester_id UUID NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            status VARCHAR(21) NOT NULL DEFAULT 'PENDING',
            approved_operation VARCHAR(16),
            approved_target_id UUID,
            approved_payload JSONB,
            approved_expected_etag TEXT,
            reviewer_id UUID,
            reviewed_at TIMESTAMPTZ,
            review_message VARCHAR(500),
            applied_change_set_id UUID,
            CONSTRAINT uq_change_requests_applied_change_set UNIQUE (applied_change_set_id),
            CONSTRAINT ck_change_requests_original CHECK ((
                (original_operation = 'CREATE'
                    AND original_target_id IS NULL AND original_expected_etag IS NULL
                    AND jsonb_typeof(original_payload) = 'object')
                OR (original_operation IN ('REFERENCE_UPDATE', 'DELETE')
                    AND original_target_id IS NOT NULL
                    AND original_expected_etag ~ '^"mc-[1-9][0-9]*-[0-9a-f]{64}"$'
                    AND ((original_operation = 'REFERENCE_UPDATE'
                            AND jsonb_typeof(original_payload) = 'object')
                        OR (original_operation = 'DELETE' AND original_payload IS NULL)))
            ) IS TRUE),
            CONSTRAINT ck_change_requests_reason CHECK (
                reason IS NULL OR (char_length(btrim(reason, ' ')) <= 500
                    AND reason !~ '[[:cntrl:]]')
            ),
            CONSTRAINT ck_change_requests_message CHECK (
                review_message IS NULL OR (char_length(review_message) BETWEEN 1 AND 500
                    AND review_message = btrim(review_message, ' ')
                    AND review_message !~ '[[:cntrl:]]')
            ),
            CONSTRAINT ck_change_requests_review CHECK ((
                (status = 'PENDING' AND reviewer_id IS NULL AND reviewed_at IS NULL
                    AND review_message IS NULL AND approved_operation IS NULL
                    AND approved_target_id IS NULL AND approved_payload IS NULL
                    AND approved_expected_etag IS NULL AND applied_change_set_id IS NULL)
                OR (status = 'REJECTED' AND reviewer_id IS NOT NULL
                    AND reviewed_at >= created_at AND review_message IS NOT NULL
                    AND approved_operation IS NULL AND approved_target_id IS NULL
                    AND approved_payload IS NULL AND approved_expected_etag IS NULL
                    AND applied_change_set_id IS NULL)
                OR (status IN ('APPROVED', 'MODIFIED_AND_APPROVED')
                    AND reviewer_id IS NOT NULL AND reviewed_at >= created_at
                    AND applied_change_set_id IS NOT NULL
                    AND substr(applied_change_set_id::text, 15, 1) = '7'
                    AND substr(applied_change_set_id::text, 20, 1) IN ('8', '9', 'a', 'b')
                    AND ((approved_operation = 'CREATE' AND approved_target_id IS NULL
                            AND approved_expected_etag IS NULL
                            AND jsonb_typeof(approved_payload) = 'object')
                        OR (approved_operation IN ('REFERENCE_UPDATE', 'DELETE', 'RESTORE')
                            AND approved_target_id IS NOT NULL
                            AND approved_expected_etag ~ '^"mc-[1-9][0-9]*-[0-9a-f]{64}"$'
                            AND ((approved_operation = 'DELETE' AND approved_payload IS NULL)
                                OR (approved_operation IN ('REFERENCE_UPDATE', 'RESTORE')
                                    AND jsonb_typeof(approved_payload) = 'object'))))
                    AND ((approved_operation = original_operation
                            AND approved_target_id IS NOT DISTINCT FROM original_target_id)
                        OR (original_operation = 'CREATE' AND approved_operation = 'RESTORE'))
                    AND ((status = 'APPROVED'
                            AND approved_operation = original_operation
                            AND approved_target_id IS NOT DISTINCT FROM original_target_id
                            AND approved_payload IS NOT DISTINCT FROM original_payload
                            AND approved_expected_etag IS NOT DISTINCT FROM original_expected_etag)
                        OR (status = 'MODIFIED_AND_APPROVED' AND review_message IS NOT NULL
                            AND (approved_operation IS DISTINCT FROM original_operation
                                OR approved_target_id IS DISTINCT FROM original_target_id
                                OR approved_payload IS DISTINCT FROM original_payload
                                OR approved_expected_etag
                                    IS DISTINCT FROM original_expected_etag))))
            ) IS TRUE)
        );
    """)
    op.execute("""
        CREATE INDEX ix_change_requests_review_queue
            ON master_code_change_requests (status, created_at, id);
    """)
    op.execute("""
        CREATE FUNCTION mdm_change_request_before_write() RETURNS trigger
        LANGUAGE plpgsql AS $function$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'change request deletion is forbidden' USING ERRCODE = '23514';
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'PENDING' THEN
                    RAISE EXCEPTION 'new request must be pending' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status <> 'PENDING' OR NEW.status = 'PENDING'
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.original_operation IS DISTINCT FROM OLD.original_operation
                OR NEW.original_target_id IS DISTINCT FROM OLD.original_target_id
                OR NEW.original_payload IS DISTINCT FROM OLD.original_payload
                OR NEW.original_expected_etag IS DISTINCT FROM OLD.original_expected_etag
                OR NEW.requester_id IS DISTINCT FROM OLD.requester_id
                OR NEW.reason IS DISTINCT FROM OLD.reason
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'immutable request fields or final state cannot change'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $function$;
    """)
    op.execute("""
        CREATE TRIGGER trg_change_request_before_write
            BEFORE INSERT OR UPDATE OR DELETE ON master_code_change_requests
            FOR EACH ROW EXECUTE FUNCTION mdm_change_request_before_write();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE master_code_change_requests")
    op.execute("DROP FUNCTION mdm_change_request_before_write()")
