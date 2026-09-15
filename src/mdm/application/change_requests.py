"""MasterCode request use cases and transaction-oriented persistence ports."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationAction, AuthorizationPolicy
from mdm.application.master_codes import (
    DimensionSelection,
    ExistingDimension,
    InlineDimension,
    MasterCodeCreateInput,
    MemoryCreateValue,
    NotApplicable,
    _normalize_plan,
)
from mdm.domain.audit import (
    AuditInvariantError,
    DimensionOperation,
    MasterCodeOperation,
    MutationAuditMetadata,
    normalize_reason,
)
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequest,
    ChangeRequestStatus,
    ChangeRequestValidationError,
    ReviewMessage,
)
from mdm.domain.master_codes import MasterCodeDimensions


class ChangeRequestNotFound(RuntimeError):
    """The request does not exist or is not visible to this requester."""


class ChangeRequestNoEffect(RuntimeError):
    """The proposal would not change data; explicit rejection is required."""


class ChangeRequestStaleTarget(RuntimeError):
    """The target aggregate changed after the selected precondition."""


class ChangeRequestRestoreMismatch(RuntimeError):
    """The proposed restore selections do not match the deleted target."""


@dataclass(frozen=True, slots=True)
class ChangeRequestCursor:
    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class ChangeRequestPage:
    items: tuple[ChangeRequest, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class ValidatedProposal:
    """Normalized selections, kept separate from the original input snapshot."""

    dimensions: tuple[tuple[str, DimensionSelection], ...]


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    proposal: ChangeProposal
    validated: ValidatedProposal
    audit: MutationAuditMetadata
    message: ReviewMessage | None


class ChangeRequestRepository(Protocol):
    async def submit(
        self, proposal: ChangeProposal, requester_id: UUID, reason: str | None
    ) -> ChangeRequest: ...

    async def get(self, request_id: UUID, *, requester_id: UUID | None) -> ChangeRequest: ...

    async def list(
        self,
        *,
        requester_id: UUID | None,
        status: ChangeRequestStatus | None,
        operation: ChangeOperation | None,
        after: ChangeRequestCursor | None,
        limit: int,
    ) -> ChangeRequestPage: ...

    async def approve(
        self, request_id: UUID, prepare: Callable[[ChangeRequest], ApprovalPlan]
    ) -> ChangeRequest:
        """Lock the request, prepare and atomically apply data, logs and its final state."""
        ...

    async def reject(
        self, request_id: UUID, reviewer_id: UUID, message: ReviewMessage
    ) -> ChangeRequest: ...


class ChangeRequestUseCases:
    """Authorize request commands and queries without accessing database sessions."""

    def __init__(
        self,
        repository: ChangeRequestRepository,
        authorization: AuthorizationPolicy,
        audit_factory: HumanMutationAuditFactory,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit_factory = audit_factory

    async def submit(
        self, principal: HumanPrincipal, proposal: ChangeProposal, *, reason: str | None
    ) -> ChangeRequest:
        action = {
            ChangeOperation.CREATE: AuthorizationAction.SUBMIT_CREATE_REQUEST,
            ChangeOperation.REFERENCE_UPDATE: AuthorizationAction.SUBMIT_REFERENCE_UPDATE_REQUEST,
            ChangeOperation.DELETE: AuthorizationAction.SUBMIT_DELETE_REQUEST,
        }.get(proposal.operation)
        if action is None:
            raise ChangeRequestValidationError("사용자는 RESTORE 요청을 제출할 수 없습니다.")
        self._authorization.authorize(principal, action)
        validate_proposal(proposal)
        try:
            normalize_reason(reason)
        except AuditInvariantError as error:
            raise ChangeRequestValidationError("유효한 요청 사유를 입력해야 합니다.") from error
        return await self._repository.submit(proposal, principal.user_id, reason)

    async def get_own(self, principal: HumanPrincipal, request_id: UUID) -> ChangeRequest:
        self._authorization.authorize(principal, AuthorizationAction.READ_DATA)
        return await self._repository.get(request_id, requester_id=principal.user_id)

    async def get_for_review(self, principal: HumanPrincipal, request_id: UUID) -> ChangeRequest:
        self._authorization.authorize(principal, AuthorizationAction.REVIEW_CHANGE_REQUEST)
        return await self._repository.get(request_id, requester_id=None)

    async def list(
        self,
        principal: HumanPrincipal,
        *,
        for_review: bool,
        status: ChangeRequestStatus | None,
        operation: ChangeOperation | None,
        after: ChangeRequestCursor | None,
        limit: int,
    ) -> ChangeRequestPage:
        self._authorization.authorize(
            principal,
            AuthorizationAction.REVIEW_CHANGE_REQUEST
            if for_review
            else AuthorizationAction.READ_DATA,
        )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ChangeRequestValidationError("limit은 1~100 정수여야 합니다.")
        if operation is ChangeOperation.RESTORE:
            raise ChangeRequestValidationError("원본 operation 필터에는 RESTORE가 없습니다.")
        return await self._repository.list(
            requester_id=None if for_review else principal.user_id,
            status=status,
            operation=operation,
            after=after,
            limit=limit,
        )

    async def approve(
        self,
        principal: HumanPrincipal,
        request_id: UUID,
        *,
        approved_proposal: ChangeProposal | None,
        message: ReviewMessage | None,
    ) -> ChangeRequest:
        self._authorization.authorize(principal, AuthorizationAction.REVIEW_CHANGE_REQUEST)

        def prepare(request: ChangeRequest) -> ApprovalPlan:
            request.require_pending()
            proposal = request.original if approved_proposal is None else approved_proposal
            request.classify_approval(proposal, message)
            validated = validate_proposal(proposal)
            has_inline = any(
                isinstance(value, InlineDimension) for _, value in validated.dimensions
            )
            audit = self._audit_factory.create(
                principal,
                dimension_operation=DimensionOperation.CREATE if has_inline else None,
                master_code_operation=MasterCodeOperation(proposal.operation.value),
                reason=message.value if message else None,
            )
            return ApprovalPlan(proposal, validated, audit, message)

        return await self._repository.approve(request_id, prepare)

    async def reject(
        self, principal: HumanPrincipal, request_id: UUID, *, message: ReviewMessage
    ) -> ChangeRequest:
        self._authorization.authorize(principal, AuthorizationAction.REVIEW_CHANGE_REQUEST)
        return await self._repository.reject(request_id, principal.user_id, message)


def validate_proposal(proposal: ChangeProposal) -> ValidatedProposal:
    """Validate input and normalize a copy without looking up referenced records."""
    if proposal.expected_etag is not None and not re.fullmatch(
        r'"mc-[1-9][0-9]*-[0-9a-f]{64}"', proposal.expected_etag, flags=re.ASCII
    ):
        raise ChangeRequestValidationError("expected_etag에는 강한 MasterCode ETag가 필요합니다.")
    if proposal.operation is ChangeOperation.DELETE:
        return ValidatedProposal(())
    assert proposal.payload is not None
    payload = proposal.payload.to_dict()
    if set(payload) != {"dimensions"} or not isinstance(payload["dimensions"], dict):
        raise ChangeRequestValidationError("payload에는 dimensions 객체가 필요합니다.")
    dimensions = payload["dimensions"]
    slots = set(MasterCodeDimensions.ORDER)
    if not dimensions or not set(dimensions) <= slots:
        raise ChangeRequestValidationError("변경할 Dimension 자리를 입력해야 합니다.")
    if (
        proposal.operation in {ChangeOperation.CREATE, ChangeOperation.RESTORE}
        and set(dimensions) != slots
    ):
        raise ChangeRequestValidationError("8개 Dimension 자리를 모두 입력해야 합니다.")
    selections: dict[str, DimensionSelection] = dict.fromkeys(
        MasterCodeDimensions.ORDER, NotApplicable()
    )
    for slot, value in dimensions.items():
        if not isinstance(value, dict):
            raise ChangeRequestValidationError("각 자리에는 mode를 포함한 객체가 필요합니다.")
        mode = value.get("mode")
        if mode == "NOT_APPLICABLE" and set(value) == {"mode"}:
            selections[slot] = NotApplicable()
        elif mode == "REFERENCE" and set(value) == {"mode", "id"}:
            try:
                selections[slot] = ExistingDimension(UUID(value["id"]))
            except (ValueError, TypeError, AttributeError):
                raise ChangeRequestValidationError("참조 id에는 UUID가 필요합니다.") from None
        elif mode == "CREATE" and set(value) == {"mode", "code", "value"}:
            if proposal.operation is ChangeOperation.RESTORE:
                raise ChangeRequestValidationError(
                    "복원 적용안에는 신규 Dimension을 생성할 수 없습니다."
                )
            raw_value = value["value"]
            if slot == "memory":
                if not isinstance(raw_value, dict) or set(raw_value) != {"amount", "unit"}:
                    raise ChangeRequestValidationError(
                        "Memory value에는 amount와 unit이 필요합니다."
                    )
                raw_value = MemoryCreateValue(raw_value["amount"], raw_value["unit"])
            selections[slot] = InlineDimension(value["code"], raw_value)
        else:
            raise ChangeRequestValidationError("mode별 필수 필드만 입력해야 합니다.")
    plan = _normalize_plan(MasterCodeCreateInput(**selections))
    return ValidatedProposal(
        tuple(
            (slot, getattr(plan, slot)) for slot in MasterCodeDimensions.ORDER if slot in dimensions
        )
    )
