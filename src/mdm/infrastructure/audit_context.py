"""SQLAlchemy adapter for PostgreSQL transaction-local mutation audit context."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.domain.audit import MutationAuditMetadata

_SET_CONTEXT = text(
    """
    WITH mutation_clock AS (
        SELECT CASE
            WHEN CAST(:minimum_timestamp AS TIMESTAMPTZ) IS NULL THEN clock_timestamp()
            ELSE GREATEST(clock_timestamp(), CAST(:minimum_timestamp AS TIMESTAMPTZ))
        END AS mutation_timestamp
    )
    SELECT
        set_config('mdm.change_set_id', :change_set_id, true),
        set_config('mdm.actor_kind', :actor_kind, true),
        set_config('mdm.actor_role', COALESCE(CAST(:actor_role AS TEXT), ''), true),
        set_config('mdm.actor_id', :actor_id, true),
        set_config('mdm.reason', COALESCE(CAST(:reason AS TEXT), ''), true),
        set_config(
            'mdm.dimension_operation',
            COALESCE(CAST(:dimension_operation AS TEXT), ''),
            true
        ),
        set_config(
            'mdm.master_code_operation',
            COALESCE(CAST(:master_code_operation AS TEXT), ''),
            true
        ),
        set_config('mdm.mutation_timestamp', mutation_timestamp::TEXT, true),
        mutation_timestamp
    FROM mutation_clock
    """
)


class SqlAlchemyMutationAuditContextWriter:
    """Install one complete audit context in a caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_context(
        self,
        metadata: MutationAuditMetadata,
        *,
        minimum_timestamp: datetime | None = None,
    ) -> datetime:
        """Set every value with PostgreSQL transaction-local semantics."""
        if minimum_timestamp is not None and minimum_timestamp.utcoffset() is None:
            raise ValueError("minimum mutation timestamp must be timezone-aware")

        actor_role = metadata.actor.role
        dimension_operation = metadata.operations.dimension
        master_code_operation = metadata.operations.master_code
        try:
            row = (
                (
                    await self._session.execute(
                        _SET_CONTEXT,
                        {
                            "change_set_id": str(metadata.change_set_id),
                            "actor_kind": metadata.actor.kind.value,
                            "actor_role": None if actor_role is None else actor_role.value,
                            "actor_id": metadata.actor.actor_id,
                            "reason": metadata.reason,
                            "dimension_operation": (
                                None if dimension_operation is None else dimension_operation.value
                            ),
                            "master_code_operation": (
                                None
                                if master_code_operation is None
                                else master_code_operation.value
                            ),
                            "minimum_timestamp": minimum_timestamp,
                        },
                    )
                )
                .mappings()
                .one()
            )
        except SQLAlchemyError:
            raise MutationAuditContextUnavailable(
                "mutation audit context could not be installed"
            ) from None

        mutation_timestamp = row["mutation_timestamp"]
        if not isinstance(mutation_timestamp, datetime) or mutation_timestamp.utcoffset() is None:
            raise MutationAuditContextUnavailable("database returned an invalid mutation timestamp")
        return mutation_timestamp
