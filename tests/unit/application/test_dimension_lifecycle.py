from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.dimension_lifecycle import (
    CompanyLifecycleRepository,
    DeleteDimension,
    GetDimensionTombstone,
    RestoreDimension,
)
from mdm.domain.audit import DimensionOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, Dimension, DimensionCode, DimensionValidationError

pytestmark = pytest.mark.unit


class RecordingLifecycle(CompanyLifecycleRepository):
    def __init__(self) -> None:
        now = datetime(2026, 9, 15, tzinfo=UTC)
        self.dimension = Dimension(
            uuid4(), DimensionCode("A1"), CompanyValue("A"), 1, now, now, None
        )
        self.calls: list[tuple[str, MutationAuditMetadata | None]] = []

    async def get_tombstone(self, dimension_id):
        self.calls.append(("get", None))
        return self.dimension

    async def delete(self, dimension_id, expected_version, audit):
        self.calls.append(("delete", audit))
        return self.dimension

    async def restore(self, dimension_id, expected_version, audit):
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
            return await GetDimensionTombstone(repo, policy).execute(principal, repo.dimension.id)
        cls = DeleteDimension if action == "delete" else RestoreDimension
        return await cls(repo, policy, factory).execute(
            principal, repo.dimension.id, expected_version=1, reason="  사유  "
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
        assert audit.operations.dimension == (
            DimensionOperation.DELETE if action == "delete" else DimensionOperation.RESTORE
        )
        assert audit.operations.master_code is None


@pytest.mark.parametrize("cls", [DeleteDimension, RestoreDimension])
async def test_invalid_reason_and_version_never_reach_lifecycle_repository(cls):
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
            await usecase.execute(principal, repo.dimension.id, expected_version=1, reason=reason)
    for version in (0, -1, True):
        with pytest.raises(ValueError):
            await usecase.execute(
                principal, repo.dimension.id, expected_version=version, reason=None
            )
    assert repo.calls == []
