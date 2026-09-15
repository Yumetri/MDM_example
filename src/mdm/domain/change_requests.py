"""Framework-independent values for MasterCode change request reviews."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mdm.domain.audit import AuditInvariantError, normalize_reason


class ChangeRequestValidationError(ValueError):
    """A change request proposal or review violates the input contract."""


class ChangeRequestAlreadyReviewed(RuntimeError):
    """A terminal request cannot be reviewed again."""


class ChangeOperation(StrEnum):
    CREATE = "CREATE"
    REFERENCE_UPDATE = "REFERENCE_UPDATE"
    DELETE = "DELETE"
    RESTORE = "RESTORE"


class ChangeRequestStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFIED_AND_APPROVED = "MODIFIED_AND_APPROVED"


@dataclass(frozen=True, slots=True)
class ProposalPayload:
    """An immutable JSON object preserving the validated user's input values."""

    encoded: str

    def __post_init__(self) -> None:
        try:
            value = json.loads(self.encoded)
            if not isinstance(value, dict):
                raise ValueError("proposal payload must be an object")
            encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ChangeRequestValidationError("요청 내용은 JSON 객체여야 합니다.") from error
        object.__setattr__(self, "encoded", encoded)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ProposalPayload":
        """Capture nested values without retaining a caller's mutable objects."""
        return cls(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))

    def to_dict(self) -> dict[str, object]:
        """Return a fresh copy for serialization or validation."""
        return json.loads(self.encoded)


@dataclass(frozen=True, slots=True)
class ReviewMessage:
    """A nonempty review explanation retained for the requester and audit."""

    value: str

    def __post_init__(self) -> None:
        try:
            normalized = normalize_reason(self.value)
        except AuditInvariantError as error:
            raise ChangeRequestValidationError("유효한 검토 메시지를 입력해야 합니다.") from error
        if normalized is None:
            raise ChangeRequestValidationError("검토 메시지는 비어 있을 수 없습니다.")
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    """One immutable proposal, separate from the state of referenced records."""

    operation: ChangeOperation
    target_id: UUID | None
    payload: ProposalPayload | None
    expected_etag: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ChangeOperation):
            raise ChangeRequestValidationError("유효한 작업 종류를 입력해야 합니다.")
        if self.operation is ChangeOperation.CREATE:
            if self.target_id is not None or self.expected_etag is not None:
                raise ChangeRequestValidationError("CREATE에는 대상과 expected ETag가 없습니다.")
        elif not isinstance(self.target_id, UUID) or not self.expected_etag:
            raise ChangeRequestValidationError("대상 UUID와 expected ETag가 필요합니다.")
        if self.operation is ChangeOperation.DELETE:
            if self.payload is not None:
                raise ChangeRequestValidationError("DELETE에는 Dimension 입력이 없습니다.")
        elif not isinstance(self.payload, ProposalPayload):
            raise ChangeRequestValidationError("Dimension 입력이 필요합니다.")


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """An immutable request snapshot with one irreversible review transition."""

    id: UUID
    original: ChangeProposal
    requester_id: UUID
    created_at: datetime
    reason: str | None = None
    status: ChangeRequestStatus = ChangeRequestStatus.PENDING
    approved: ChangeProposal | None = None
    reviewer_id: UUID | None = None
    review_message: ReviewMessage | None = None
    reviewed_at: datetime | None = None
    applied_change_set_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.original.operation is ChangeOperation.RESTORE:
            raise ChangeRequestValidationError("사용자는 RESTORE 요청을 제출할 수 없습니다.")
        if self.created_at.utcoffset() is None:
            raise ChangeRequestValidationError("제출 시각에는 시간대가 필요합니다.")
        if not isinstance(self.status, ChangeRequestStatus):
            raise ChangeRequestValidationError("유효한 요청 상태가 필요합니다.")
        if self.status is ChangeRequestStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.approved,
                    self.reviewer_id,
                    self.review_message,
                    self.reviewed_at,
                    self.applied_change_set_id,
                )
            ):
                raise ChangeRequestValidationError("PENDING 요청에는 검토 결과가 없습니다.")
            return
        if (
            not isinstance(self.reviewer_id, UUID)
            or self.reviewed_at is None
            or self.reviewed_at.utcoffset() is None
            or self.reviewed_at < self.created_at
        ):
            raise ChangeRequestValidationError("검토자와 제출 이후의 검토 시각이 필요합니다.")
        if self.status is ChangeRequestStatus.REJECTED:
            if (
                self.review_message is None
                or self.approved is not None
                or self.applied_change_set_id is not None
            ):
                raise ChangeRequestValidationError(
                    "거절에는 메시지만 기록하며 변경을 적용하지 않습니다."
                )
            return
        if (
            self.approved is None
            or not isinstance(self.applied_change_set_id, UUID)
            or self.applied_change_set_id.version != 7
        ):
            raise ChangeRequestValidationError("승인에는 적용안과 UUIDv7 change set이 필요합니다.")
        expected_status = self.classify_approval(self.approved, self.review_message)
        if self.status is not expected_status:
            raise ChangeRequestValidationError(
                "승인 상태가 원안과 적용안의 차이와 일치하지 않습니다."
            )

    def require_pending(self) -> None:
        if self.status is not ChangeRequestStatus.PENDING:
            raise ChangeRequestAlreadyReviewed

    def classify_approval(
        self, proposal: ChangeProposal, message: ReviewMessage | None
    ) -> ChangeRequestStatus:
        restore_exception = (
            self.original.operation is ChangeOperation.CREATE
            and proposal.operation is ChangeOperation.RESTORE
        )
        if not restore_exception and (
            proposal.operation is not self.original.operation
            or proposal.target_id != self.original.target_id
        ):
            raise ChangeRequestValidationError("승인 시 operation과 대상을 바꿀 수 없습니다.")
        if proposal == self.original:
            return ChangeRequestStatus.APPROVED
        if message is None:
            raise ChangeRequestValidationError("수정 승인에는 검토 메시지가 필요합니다.")
        return ChangeRequestStatus.MODIFIED_AND_APPROVED

    def approve(
        self,
        proposal: ChangeProposal,
        reviewer_id: UUID,
        reviewed_at: datetime,
        change_set_id: UUID,
        message: ReviewMessage | None,
    ) -> "ChangeRequest":
        self.require_pending()
        return replace(
            self,
            status=self.classify_approval(proposal, message),
            approved=proposal,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            applied_change_set_id=change_set_id,
            review_message=message,
        )

    def reject(
        self, reviewer_id: UUID, reviewed_at: datetime, message: ReviewMessage
    ) -> "ChangeRequest":
        self.require_pending()
        return replace(
            self,
            status=ChangeRequestStatus.REJECTED,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            review_message=message,
        )
