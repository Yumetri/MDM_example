from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.master_code_lifecycle import (
    DeleteMasterCode,
    GetMasterCodeTombstone,
    MasterCodeLifecycleRepository,
    RestoreMasterCode,
)
from mdm.domain.audit import MasterCodeOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import DimensionValidationError
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions

pytestmark = pytest.mark.unit


class RecordingLifecycle(MasterCodeLifecycleRepository):
    def __init__(self) -> None:
        now = datetime(2026, 9, 15, tzinfo=UTC)
        self.dimension = MasterCode.create(
            id=uuid4(), dimensions=MasterCodeDimensions(), created_at=now
        )
        self.calls: list[tuple[str, MutationAuditMetadata | None]] = []

    async def get_tombstone(self, dimension_id):
        self.calls.append(("get", None))
        return self.dimension

    async def delete(self, dimension_id, expected_etag, audit):
        self.calls.append(("delete", audit))
        return self.dimension

    async def restore(self, dimension_id, expected_etag, audit):
        self.calls.append(("restore", audit))
        return self.dimension


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("action", ["delete", "restore", "get"])
async def test_lifecycle_usecases_authorize_before_repository_and_preserve_trusted_audit(
    role, action
):
    repo = RecordingLifecycle()
    principal = HumanPrincipal(uuid4(), role)
    change_set = UUID("01890f7c-8abc-7def-8abc-444444444444")
    factory = HumanMutationAuditFactory(change_set_ids=lambda: change_set)
    policy = AuthorizationPolicy()

    async def execute():
        if action == "get":
            return await GetMasterCodeTombstone(repo, policy).execute(principal, repo.dimension.id)
        cls = DeleteMasterCode if action == "delete" else RestoreMasterCode
        return await cls(repo, policy, factory).execute(
            principal, repo.dimension.id, expected_etag=repo.dimension.etag(), reason="  사유  "
        )

    if role == UserRole.USER:
        with pytest.raises(AuthorizationDenied):
            await execute()
        assert repo.calls == []
        return
    assert await execute() == repo.dimension
    assert len(repo.calls) == 1
    recorded_action, audit = repo.calls[0]
    assert recorded_action == action
    if action == "get":
        assert audit is None
    else:
        assert audit is not None
        assert audit.actor.actor_id == str(principal.user_id)
        assert audit.actor.role == role
        assert audit.change_set_id == change_set
        assert audit.reason == "사유"
        assert audit.operations.master_code == (
            MasterCodeOperation.DELETE if action == "delete" else MasterCodeOperation.RESTORE
        )
        assert audit.operations.dimension is None


@pytest.mark.parametrize("cls", [DeleteMasterCode, RestoreMasterCode])
async def test_invalid_reason_never_reaches_lifecycle_repository(cls):
    repo = RecordingLifecycle()
    usecase = cls(
        repo,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(
            change_set_ids=lambda: UUID("01890f7c-8abc-7def-8abc-444444444444")
        ),
    )
    principal = HumanPrincipal(uuid4(), UserRole.ADMIN)
    for reason in ("a\tb", "a\nb", "a" * 501):
        with pytest.raises(DimensionValidationError):
            await usecase.execute(
                principal, repo.dimension.id, expected_etag=repo.dimension.etag(), reason=reason
            )
    assert repo.calls == []
