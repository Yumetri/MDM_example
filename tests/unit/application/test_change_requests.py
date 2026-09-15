from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.change_requests import ChangeRequestUseCases, validate_proposal
from mdm.domain.auth import UserRole
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequest,
    ProposalPayload,
)

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
REQUEST_ID = UUID("00000000-0000-7000-8000-000000000002")
CHANGE_SET_ID = UUID("00000000-0000-7000-8000-000000000003")


class SubmissionRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[ChangeProposal, UUID, str | None]] = []

    async def submit(
        self, proposal: ChangeProposal, requester_id: UUID, reason: str | None
    ) -> ChangeRequest:
        self.calls.append((proposal, requester_id, reason))
        return ChangeRequest(
            id=REQUEST_ID,
            original=proposal,
            requester_id=requester_id,
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            reason=reason,
        )


def _use_cases(repository: SubmissionRepository) -> ChangeRequestUseCases:
    return ChangeRequestUseCases(
        repository,  # type: ignore[bad-argument-type]
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=lambda: CHANGE_SET_ID),
    )


def _proposal() -> ChangeProposal:
    slots = {
        slot: {"mode": "NOT_APPLICABLE"}
        for slot in (
            "company",
            "brand",
            "model",
            "category",
            "year",
            "memory",
            "network",
            "country",
        )
    }
    slots["company"] = {"mode": "CREATE", "code": "acme", "value": "  Acme  "}
    return ChangeProposal(
        ChangeOperation.CREATE, None, ProposalPayload.from_dict({"dimensions": slots}), None
    )


async def test_submission_preserves_raw_payload_and_only_calls_request_storage() -> None:
    repository = SubmissionRepository()
    proposal = _proposal()
    result = await _use_cases(repository).submit(
        HumanPrincipal(USER_ID, UserRole.USER), proposal, reason=None
    )

    assert result.original == proposal
    assert repository.calls == [(proposal, USER_ID, None)]
    assert "acme" in result.original.payload.encoded  # type: ignore[missing-attribute]
    assert "ACME" in str(validate_proposal(proposal))


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
async def test_admin_cannot_submit_even_a_valid_request(role: UserRole) -> None:
    repository = SubmissionRepository()
    with pytest.raises(AuthorizationDenied):
        await _use_cases(repository).submit(HumanPrincipal(USER_ID, role), _proposal(), reason=None)
    assert repository.calls == []


async def test_submission_does_not_resolve_nonexistent_target_or_stale_etag() -> None:
    repository = SubmissionRepository()
    proposal = ChangeProposal(ChangeOperation.DELETE, REQUEST_ID, None, '"mc-1-' + "0" * 64 + '"')
    result = await _use_cases(repository).submit(
        HumanPrincipal(USER_ID, UserRole.USER), proposal, reason=None
    )
    assert result.original.target_id == REQUEST_ID
