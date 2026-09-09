from typing import cast
from unittest.mock import Mock
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.domain.audit import (
    ActorKind,
    AuditInvariantError,
    DimensionOperation,
    MasterCodeOperation,
)
from mdm.domain.auth import UserRole

CHANGE_SET_ID = UUID("01890f7c-8abc-7def-8abc-0123456789ab")
USER_ID = UUID("01890f7c-8abc-7def-8abc-abcdefabcdef")


@pytest.mark.unit
def test_factory_maps_only_a_verified_human_principal_to_audit_metadata() -> None:
    factory = HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID)

    metadata = factory.create(
        HumanPrincipal(user_id=USER_ID, role=UserRole.SUPER_ADMIN),
        dimension_operation=DimensionOperation.UPDATE,
        master_code_operation=MasterCodeOperation.RECOMPOSE,
        reason="  코드 변경  ",
    )

    assert metadata.change_set_id == CHANGE_SET_ID
    assert metadata.actor.kind is ActorKind.HUMAN
    assert metadata.actor.actor_id == str(USER_ID)
    assert metadata.actor.role is UserRole.SUPER_ADMIN
    assert metadata.reason == "코드 변경"


@pytest.mark.unit
def test_factory_rejects_an_unvalidated_role_value() -> None:
    factory = HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID)
    principal = HumanPrincipal(user_id=USER_ID, role=cast(UserRole, "SYSTEM"))

    with pytest.raises(AuditInvariantError):
        factory.create(principal, dimension_operation=DimensionOperation.CREATE)


@pytest.mark.unit
def test_factory_generates_exactly_one_change_set_id_per_mutation() -> None:
    change_set_ids = Mock(return_value=CHANGE_SET_ID)
    factory = HumanMutationAuditFactory(change_set_ids=change_set_ids)

    metadata = factory.create(
        HumanPrincipal(user_id=USER_ID, role=UserRole.ADMIN),
        dimension_operation=DimensionOperation.CREATE,
    )

    assert metadata.change_set_id == CHANGE_SET_ID
    change_set_ids.assert_called_once_with()
