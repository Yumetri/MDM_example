"""Conditional MasterCode lifecycle commands and persistence boundary."""

from typing import Protocol
from uuid import UUID

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.domain.audit import AuditInvariantError, MasterCodeOperation, MutationAuditMetadata
from mdm.domain.dimensions import DimensionValidationError
from mdm.domain.master_codes import MasterCode


class MasterCodeNotDeleted(RuntimeError):
    """The requested tombstone or restoration target is active."""


class MasterCodeReferenceInactive(RuntimeError):
    """Restoration cannot reuse a deleted Dimension reference."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__()
        self.fields = fields


class MasterCodeLifecycleRepository(Protocol):
    async def get_tombstone(self, master_code_id: UUID) -> MasterCode: ...

    async def delete(
        self, master_code_id: UUID, expected_etag: str, audit: MutationAuditMetadata
    ) -> MasterCode: ...

    async def restore(
        self, master_code_id: UUID, expected_etag: str, audit: MutationAuditMetadata
    ) -> MasterCode: ...


class GetMasterCodeTombstone:
    def __init__(
        self, repository: MasterCodeLifecycleRepository, authorization: AuthorizationPolicy
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    async def execute(self, principal: HumanPrincipal, master_code_id: UUID) -> MasterCode:
        self._authorization.authorize(principal, AuthorizationAction.READ_TOMBSTONE)
        return await self._repository.get_tombstone(master_code_id)


class _MutateMasterCodeLifecycle:
    def __init__(
        self,
        repository: MasterCodeLifecycleRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
        operation: MasterCodeOperation,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory
        self._operation = operation

    async def execute(
        self,
        principal: HumanPrincipal,
        master_code_id: UUID,
        *,
        expected_etag: str,
        reason: str | None,
    ) -> MasterCode:
        self._authorization.authorize(principal, AuthorizationAction.MUTATE_DATA)
        try:
            audit = self._audit_factory.create(
                principal, master_code_operation=self._operation, reason=reason
            )
        except AuditInvariantError as error:
            raise DimensionValidationError(
                "reason", "유효한 변경 사유를 입력해야 합니다."
            ) from error
        if self._operation == MasterCodeOperation.DELETE:
            return await self._repository.delete(master_code_id, expected_etag, audit)
        return await self._repository.restore(master_code_id, expected_etag, audit)


class DeleteMasterCode(_MutateMasterCodeLifecycle):
    def __init__(
        self,
        repository: MasterCodeLifecycleRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, MasterCodeOperation.DELETE)


class RestoreMasterCode(_MutateMasterCodeLifecycle):
    def __init__(
        self,
        repository: MasterCodeLifecycleRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        super().__init__(repository, authorization, audit_factory, MasterCodeOperation.RESTORE)
