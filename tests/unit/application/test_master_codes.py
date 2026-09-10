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
    MemoryCreateValue,
    NotApplicable,
)
from mdm.domain.audit import DimensionOperation, MasterCodeOperation
from mdm.domain.auth import UserRole
from mdm.domain.dimensions import CompanyValue, DimensionCode, MemoryUnit, MemoryValue
from mdm.domain.master_codes import MasterCode, MasterCodeDimensions

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ADMIN = HumanPrincipal(user_id=UUID("00000000-0000-7000-8000-000000000001"), role=UserRole.ADMIN)
USER = HumanPrincipal(user_id=UUID("00000000-0000-7000-8000-000000000002"), role=UserRole.USER)
CHANGE_SET_ID = UUID("00000000-0000-7000-8000-000000000003")


class FakeRepository:
    def __init__(self) -> None:
        self.received: tuple[MasterCodeCreatePlan, object] | None = None

    async def create(self, plan: MasterCodeCreatePlan, audit: object) -> MasterCode:
        self.received = (plan, audit)
        return MasterCode.create(
            id=UUID("00000000-0000-7000-8000-000000000004"),
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
