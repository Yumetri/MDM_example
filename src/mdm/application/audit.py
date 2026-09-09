"""Application factory for trusted HUMAN mutation audit metadata."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.auth import HumanPrincipal
from mdm.domain.audit import (
    AuditActor,
    DimensionOperation,
    MasterCodeOperation,
    MutationAuditMetadata,
    MutationOperations,
)


class MutationAuditContextUnavailable(RuntimeError):
    """The database audit context could not be installed safely."""


class MutationAuditContextWriter(Protocol):
    """Set validated audit metadata in one caller-owned DB transaction."""

    async def set_context(
        self,
        metadata: MutationAuditMetadata,
        *,
        minimum_timestamp: datetime | None = None,
    ) -> datetime:
        """Return the DB timestamp stored in the transaction-local context."""
        ...


class HumanMutationAuditFactory:
    """Map only a verified HUMAN Principal into mutation audit metadata."""

    def __init__(self, *, change_set_ids: Callable[[], UUID]) -> None:
        self._change_set_ids = change_set_ids

    def create(
        self,
        principal: HumanPrincipal,
        *,
        dimension_operation: DimensionOperation | None = None,
        master_code_operation: MasterCodeOperation | None = None,
        reason: str | None = None,
    ) -> MutationAuditMetadata:
        """Create one change set shared by every entity in this mutation."""
        actor = AuditActor.human(user_id=principal.user_id, role=principal.role)
        operations = MutationOperations(
            dimension=dimension_operation,
            master_code=master_code_operation,
        )
        return MutationAuditMetadata(
            change_set_id=self._change_set_ids(),
            actor=actor,
            operations=operations,
            reason=reason,
        )
