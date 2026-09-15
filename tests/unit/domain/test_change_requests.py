from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequest,
    ChangeRequestAlreadyReviewed,
    ChangeRequestStatus,
    ChangeRequestValidationError,
    ProposalPayload,
    ReviewMessage,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)
REQUEST_ID = UUID("00000000-0000-7000-8000-000000000001")
USER_ID = UUID("00000000-0000-7000-8000-000000000002")
ADMIN_ID = UUID("00000000-0000-7000-8000-000000000003")
CHANGE_SET_ID = UUID("00000000-0000-7000-8000-000000000004")
TARGET_ID = UUID("00000000-0000-7000-8000-000000000005")


def _request(proposal: ChangeProposal) -> ChangeRequest:
    return ChangeRequest(id=REQUEST_ID, original=proposal, requester_id=USER_ID, created_at=NOW)


@pytest.mark.parametrize("message", ["", "   ", "\t", "사유\n", "사유\x00", "가" * 501, 1])
def test_review_message_rejects_empty_controls_non_string_and_overlong_input(
    message: object,
) -> None:
    with pytest.raises(ChangeRequestValidationError):
        ReviewMessage(message)  # type: ignore[bad-argument-type]


def test_review_message_preserves_case_unicode_and_internal_spaces() -> None:
    message = ReviewMessage("  기존 ACME 참조를  사용합니다.  ")

    assert message.value == "기존 ACME 참조를  사용합니다."


def test_review_message_applies_length_limit_after_trimming_edge_spaces() -> None:
    assert ReviewMessage(" " + "가" * 500 + " ").value == "가" * 500


def test_proposal_payload_preserves_raw_strings_and_isolates_nested_mutation() -> None:
    original = {"brand": {"mode": "CREATE", "code": "acme", "value": "  Acme  Brand  "}}
    payload = ProposalPayload.from_dict(original)
    original["brand"]["code"] = "OTHER"
    decoded = payload.to_dict()
    assert decoded == {"brand": {"mode": "CREATE", "code": "acme", "value": "  Acme  Brand  "}}
    decoded.clear()
    assert payload.to_dict() == {
        "brand": {"mode": "CREATE", "code": "acme", "value": "  Acme  Brand  "}
    }


def test_proposal_comparison_ignores_object_key_order_but_preserves_string_changes() -> None:
    raw = ProposalPayload.from_dict({"brand": {"code": "acme", "mode": "CREATE"}})
    reordered = ProposalPayload.from_dict({"brand": {"mode": "CREATE", "code": "acme"}})
    rewritten = ProposalPayload.from_dict({"brand": {"mode": "CREATE", "code": "ACME"}})

    assert raw == reordered
    assert raw != rewritten


def test_approval_retains_original_and_classifies_only_etag_change_as_modified() -> None:
    original = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"old"')
    approved = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"new"')
    pending = _request(original)
    with pytest.raises(ChangeRequestValidationError):
        pending.approve(approved, ADMIN_ID, NOW, CHANGE_SET_ID, None)

    result = pending.approve(
        approved, ADMIN_ID, NOW, CHANGE_SET_ID, ReviewMessage("현재 상태 확인")
    )

    assert result.status is ChangeRequestStatus.MODIFIED_AND_APPROVED
    assert result.original == original
    assert result.approved == approved
    assert result.applied_change_set_id == CHANGE_SET_ID
    assert pending.status is ChangeRequestStatus.PENDING
    assert pending.approved is None


def test_identical_proposal_can_be_approved_without_message() -> None:
    original = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    result = _request(original).approve(original, ADMIN_ID, NOW, CHANGE_SET_ID, None)

    assert result.status is ChangeRequestStatus.APPROVED
    assert result.review_message is None
    assert result.reviewer_id == ADMIN_ID


def test_rejection_retains_original_without_applied_proposal_or_change_set() -> None:
    original = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    result = _request(original).reject(ADMIN_ID, NOW, ReviewMessage("이미 삭제되었습니다."))

    assert result.status is ChangeRequestStatus.REJECTED
    assert result.original == original
    assert result.approved is None
    assert result.applied_change_set_id is None


@pytest.mark.parametrize("approved", [False, True])
def test_terminal_request_cannot_be_approved_or_rejected_again(approved: bool) -> None:
    proposal = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    pending = _request(proposal)
    reviewed = (
        pending.approve(proposal, ADMIN_ID, NOW, CHANGE_SET_ID, None)
        if approved
        else pending.reject(ADMIN_ID, NOW, ReviewMessage("거절합니다."))
    )
    with pytest.raises(ChangeRequestAlreadyReviewed):
        reviewed.approve(proposal, ADMIN_ID, NOW, CHANGE_SET_ID, None)
    with pytest.raises(ChangeRequestAlreadyReviewed):
        reviewed.reject(ADMIN_ID, NOW, ReviewMessage("거절합니다."))


def test_review_cannot_predate_submission() -> None:
    proposal = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    with pytest.raises(ChangeRequestValidationError):
        _request(proposal).reject(ADMIN_ID, NOW - timedelta(seconds=1), ReviewMessage("거절"))


def test_modified_approval_cannot_change_an_existing_target() -> None:
    original = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    other = ChangeProposal(ChangeOperation.DELETE, USER_ID, None, '"etag"')
    with pytest.raises(ChangeRequestValidationError):
        _request(original).approve(other, ADMIN_ID, NOW, CHANGE_SET_ID, ReviewMessage("대상 변경"))


def test_create_to_restore_is_the_only_permitted_operation_change() -> None:
    payload = ProposalPayload.from_dict({"dimensions": {}})
    original = ChangeProposal(ChangeOperation.CREATE, None, payload, None)
    restore = ChangeProposal(ChangeOperation.RESTORE, TARGET_ID, payload, '"etag"')

    result = _request(original).approve(
        restore, ADMIN_ID, NOW, CHANGE_SET_ID, ReviewMessage("기존 행 복원")
    )
    assert result.status is ChangeRequestStatus.MODIFIED_AND_APPROVED
    assert result.original.target_id is None
    assert result.approved == restore

    delete = ChangeProposal(ChangeOperation.DELETE, TARGET_ID, None, '"etag"')
    with pytest.raises(ChangeRequestValidationError):
        _request(original).approve(delete, ADMIN_ID, NOW, CHANGE_SET_ID, ReviewMessage("변경"))
