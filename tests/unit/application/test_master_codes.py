from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.master_codes import (
    CreateMasterCode,
    ExistingDimension,
    InlineDimension,
    MasterCodeCreateInput,
    MasterCodeCreatePlan,
    MasterCodeCursor,
    MasterCodePage,
    MasterCodeReferenceUpdate,
    MemoryCreateValue,
    NotApplicable,
    UpdateMasterCodeReferences,
)
from mdm.domain.audit import DimensionOperation, MasterCodeOperation, MutationAuditMetadata
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, DimensionCode, MemoryUnit, MemoryValue
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ADMIN = HumanPrincipal(user_id=UUID("00000000-0000-7000-8000-000000000001"), role=UserRole.ADMIN)
USER = HumanPrincipal(user_id=UUID("00000000-0000-7000-8000-000000000002"), role=UserRole.USER)
CHANGE_SET_ID = UUID("00000000-0000-7000-8000-000000000003")


class FakeRepository:
    def __init__(self) -> None:
        self.received: tuple[MasterCodeCreatePlan, MutationAuditMetadata] | None = None
        self.updated: tuple[UUID, MasterCodeReferenceUpdate, str, MutationAuditMetadata] | None = (
            None
        )

    async def create(self, plan: MasterCodeCreatePlan, audit: MutationAuditMetadata) -> MasterCode:
        self.received = (plan, audit)
        return MasterCode.create(
            id=UUID("00000000-0000-7000-8000-000000000004"),
            dimensions=MasterCodeDimensions(),
            created_at=NOW,
        )

    async def get_active(self, master_code_id: UUID) -> MasterCode:
        raise AssertionError(f"unexpected get_active call for {master_code_id}")

    async def list_active(
        self,
        *,
        after: MasterCodeCursor | None,
        limit: int,
    ) -> MasterCodePage:
        raise AssertionError(f"unexpected list_active call for {after=}, {limit=}")

    async def update_references(
        self,
        master_code_id: UUID,
        changes: MasterCodeReferenceUpdate,
        expected_etag: str,
        audit: MutationAuditMetadata,
    ) -> MasterCode:
        self.updated = (master_code_id, changes, expected_etag, audit)
        return MasterCode.create(
            id=master_code_id,
            dimensions=MasterCodeDimensions(),
            created_at=NOW,
        )


def _input() -> MasterCodeCreateInput:
    return MasterCodeCreateInput(
        company=InlineDimension(code="com", value="  Acme  Corp "),
        brand=ExistingDimension(UUID("00000000-0000-7000-8000-000000000010")),
        model=NotApplicable(),
        category=NotApplicable(),
        year=NotApplicable(),
        memory=InlineDimension(code="MEM128", value=MemoryCreateValue(amount=128, unit=" gb ")),
        network=NotApplicable(),
        country=NotApplicable(),
    )


@pytest.mark.unit
async def test_create_master_code_authorizes_normalizes_and_builds_one_audit_context() -> None:
    repository = FakeRepository()
    use_case = CreateMasterCode(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    await use_case.execute(ADMIN, dimensions=_input(), reason=" 등록 ")

    assert repository.received is not None
    plan, audit = repository.received
    assert plan.company == InlineDimension(
        code=DimensionCode("COM"), value=CompanyValue("ACME_CORP")
    )
    assert plan.memory == InlineDimension(
        code=DimensionCode("MEM128"),
        value=MemoryValue(amount=128, unit=MemoryUnit.GB),
    )
    assert audit.change_set_id == CHANGE_SET_ID
    assert audit.operations.dimension is DimensionOperation.CREATE
    assert audit.operations.master_code is MasterCodeOperation.CREATE
    assert audit.reason == "등록"


@pytest.mark.unit
async def test_create_master_code_denies_user_before_repository_work() -> None:
    repository = FakeRepository()
    use_case = CreateMasterCode(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    with pytest.raises(AuthorizationDenied):
        await use_case.execute(USER, dimensions=_input(), reason=None)

    assert repository.received is None


@pytest.mark.unit
async def test_update_master_code_references_authorizes_and_builds_audit_context() -> None:
    repository = FakeRepository()
    use_case = UpdateMasterCodeReferences(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )
    master_code_id = UUID("00000000-0000-7000-8000-000000000004")
    changes = MasterCodeReferenceUpdate(
        company=ExistingDimension(UUID("00000000-0000-7000-8000-000000000010")),
        country=NotApplicable(),
    )

    await use_case.execute(
        ADMIN,
        master_code_id=master_code_id,
        changes=changes,
        expected_etag='"mc-1-' + "a" * 64 + '"',
        reason=" 정정 ",
    )

    assert repository.updated is not None
    received_id, received_changes, received_etag, audit = repository.updated
    assert received_id == master_code_id
    assert received_changes is changes
    assert received_etag == '"mc-1-' + "a" * 64 + '"'
    assert audit.operations.dimension is None
    assert audit.operations.master_code is MasterCodeOperation.REFERENCE_UPDATE
    assert audit.reason == "정정"


@pytest.mark.unit
async def test_update_master_code_references_denies_user_before_repository_work() -> None:
    repository = FakeRepository()
    use_case = UpdateMasterCodeReferences(
        repository,
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )

    with pytest.raises(AuthorizationDenied):
        await use_case.execute(
            USER,
            master_code_id=UUID("00000000-0000-7000-8000-000000000004"),
            changes=MasterCodeReferenceUpdate(company=NotApplicable()),
            expected_etag='"mc-1-' + "a" * 64 + '"',
            reason=None,
        )

    assert repository.updated is None
