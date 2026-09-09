from typing import cast
from uuid import UUID

import pytest

from mdm.domain.audit import (
    ActorKind,
    AuditActor,
    AuditInvariantError,
    DimensionOperation,
    MasterCodeOperation,
    MutationAuditMetadata,
    MutationOperations,
    normalize_reason,
)
from mdm.domain.auth import UserRole


@pytest.mark.unit
def test_human_actor_requires_one_supported_role_and_canonical_uuid_id() -> None:
    user_id = UUID("01890f7c-8abc-7def-8abc-0123456789ab")

    actor = AuditActor.human(user_id=user_id, role=UserRole.ADMIN)

    assert actor.kind is ActorKind.HUMAN
    assert actor.actor_id == str(user_id)
    assert actor.role is UserRole.ADMIN
    with pytest.raises(AuditInvariantError):
        AuditActor(kind=ActorKind.HUMAN, actor_id=str(user_id), role=None)
    with pytest.raises(AuditInvariantError):
        AuditActor(
            kind=ActorKind.HUMAN,
            actor_id=str(user_id),
            role=cast(UserRole, "OWNER"),
        )


@pytest.mark.unit
def test_future_system_actor_shape_is_representable_without_a_human_role() -> None:
    actor = AuditActor(kind=ActorKind.SYSTEM, actor_id="nightly-recompose", role=None)

    assert actor.kind is ActorKind.SYSTEM
    assert actor.role is None
    with pytest.raises(AuditInvariantError):
        AuditActor(
            kind=ActorKind.SYSTEM,
            actor_id="nightly-recompose",
            role=UserRole.SUPER_ADMIN,
        )


@pytest.mark.unit
@pytest.mark.parametrize("actor_id", ["", " leading", "trailing ", "x" * 256])
def test_actor_id_rejects_invalid_storage_shape(actor_id: str) -> None:
    with pytest.raises(AuditInvariantError):
        AuditActor(kind=ActorKind.SYSTEM, actor_id=actor_id, role=None)


@pytest.mark.unit
def test_reason_normalizes_only_edge_spaces_and_rejects_control_characters() -> None:
    assert normalize_reason(None) is None
    assert normalize_reason("   ") is None
    assert normalize_reason("  변경 사유  ") == "변경 사유"
    assert normalize_reason("내부  공백") == "내부  공백"

    for invalid in ("탭\t포함", "줄바꿈\n포함", "제어\x00문자", "x" * 501):
        with pytest.raises(AuditInvariantError):
            normalize_reason(invalid)


@pytest.mark.unit
def test_operations_keep_dimension_and_master_code_meanings_independent() -> None:
    operations = MutationOperations(
        dimension=DimensionOperation.UPDATE,
        master_code=MasterCodeOperation.RECOMPOSE,
    )

    assert operations.dimension is DimensionOperation.UPDATE
    assert operations.master_code is MasterCodeOperation.RECOMPOSE
    with pytest.raises(AuditInvariantError):
        MutationOperations()


@pytest.mark.unit
def test_mutation_metadata_requires_a_uuidv7_change_set() -> None:
    actor = AuditActor.human(
        user_id=UUID("01890f7c-8abc-7def-8abc-0123456789ab"),
        role=UserRole.USER,
    )
    operations = MutationOperations(dimension=DimensionOperation.CREATE)

    metadata = MutationAuditMetadata(
        change_set_id=UUID("01890f7c-8abc-7def-8abc-0123456789ab"),
        actor=actor,
        operations=operations,
        reason="  최초 생성  ",
    )

    assert metadata.reason == "최초 생성"
    with pytest.raises(AuditInvariantError):
        MutationAuditMetadata(
            change_set_id=UUID("12345678-1234-4123-8123-123456789abc"),
            actor=actor,
            operations=operations,
        )
