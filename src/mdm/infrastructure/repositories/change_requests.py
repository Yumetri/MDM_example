"""Atomic persistence of proposals, reviews and their associated MDM mutations."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mdm.application.audit import MutationAuditContextUnavailable
from mdm.application.change_requests import (
    ApprovalPlan,
    ChangeRequestCursor,
    ChangeRequestNoEffect,
    ChangeRequestNotFound,
    ChangeRequestPage,
    ChangeRequestRestoreMismatch,
    ChangeRequestStaleTarget,
)
from mdm.application.master_code_lifecycle import MasterCodeNotDeleted, MasterCodeReferenceInactive
from mdm.application.master_codes import (
    DimensionSelection,
    ExistingDimension,
    InlineDimension,
    InvalidDimensionReference,
    MasterCodeCreatePlan,
    MasterCodeNotFound,
    MasterCodeRepositoryUnavailable,
    NotApplicable,
)
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequest,
    ChangeRequestStatus,
    ProposalPayload,
    ReviewMessage,
)
from mdm.domain.master_codes import MasterCode, MasterCodeDimension, MasterCodeDimensions
from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
from mdm.infrastructure.models import MasterCodeChangeRequestRecord, MasterCodeRecord
from mdm.infrastructure.repositories.master_codes import (
    _SLOTS,
    SqlAlchemyMasterCodeRepository,
    _dimension_ids,
    _joined_statement,
    _joined_to_domain,
    _master_code,
    _raise_if_temporarily_unavailable,
    _same_persisted_state,
)


class SqlAlchemyChangeRequestRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._master_codes = SqlAlchemyMasterCodeRepository(session_factory)

    async def submit(
        self, proposal: ChangeProposal, requester_id: UUID, reason: str | None
    ) -> ChangeRequest:
        try:
            async with self._session_factory.begin() as session:
                record = MasterCodeChangeRequestRecord(
                    **_proposal_columns(proposal, "original"),
                    requester_id=requester_id,
                    reason=reason,
                )
                session.add(record)
                await session.flush()
                return _request(record)
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def get(self, request_id: UUID, *, requester_id: UUID | None) -> ChangeRequest:
        statement = select(MasterCodeChangeRequestRecord).where(
            MasterCodeChangeRequestRecord.id == request_id
        )
        if requester_id is not None:
            statement = statement.where(MasterCodeChangeRequestRecord.requester_id == requester_id)
        try:
            async with self._session_factory() as session:
                record = await session.scalar(statement)
                if record is None:
                    raise ChangeRequestNotFound
                return _request(record)
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def list(
        self,
        *,
        requester_id: UUID | None,
        status: ChangeRequestStatus | None,
        operation: ChangeOperation | None,
        after: ChangeRequestCursor | None,
        limit: int,
    ) -> ChangeRequestPage:
        model = MasterCodeChangeRequestRecord
        statement = select(model)
        if requester_id is not None:
            statement = statement.where(model.requester_id == requester_id)
        if status is not None:
            statement = statement.where(model.status == status.value)
        if operation is not None:
            statement = statement.where(model.original_operation == operation.value)
        if after is not None:
            statement = statement.where(
                or_(
                    model.created_at < after.created_at,
                    and_(model.created_at == after.created_at, model.id < after.id),
                )
            )
        statement = statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
        try:
            async with self._session_factory() as session:
                rows = (await session.scalars(statement)).all()
                return ChangeRequestPage(
                    tuple(_request(row) for row in rows[:limit]), len(rows) > limit
                )
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def approve(
        self, request_id: UUID, prepare: Callable[[ChangeRequest], ApprovalPlan]
    ) -> ChangeRequest:
        plan: ApprovalPlan | None = None
        try:
            async with self._session_factory.begin() as session:
                record = await _lock_request(session, request_id)
                original = _request(record)
                plan = prepare(original)
                result = await self._apply(session, plan)
                reviewed_at = await session.scalar(
                    func.greatest(func.clock_timestamp(), original.created_at, result.updated_at)
                )
                reviewed = original.approve(
                    plan.proposal,
                    UUID(plan.audit.actor.actor_id),
                    reviewed_at,
                    plan.audit.change_set_id,
                    plan.message,
                )
                _set_review(record, reviewed)
                await session.flush()
                return reviewed
        except IntegrityError as error:
            if plan is not None:
                selections: dict[str, DimensionSelection] = dict.fromkeys(
                    MasterCodeDimensions.ORDER, NotApplicable()
                )
                selections.update(dict(plan.validated.dimensions))
                await self._master_codes._translate_integrity_error(
                    error, MasterCodeCreatePlan(**selections)
                )
            raise
        except MutationAuditContextUnavailable:
            raise MasterCodeRepositoryUnavailable from None
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def reject(
        self, request_id: UUID, reviewer_id: UUID, message: ReviewMessage
    ) -> ChangeRequest:
        try:
            async with self._session_factory.begin() as session:
                record = await _lock_request(session, request_id)
                original = _request(record)
                reviewed_at = await session.scalar(
                    func.greatest(func.clock_timestamp(), original.created_at)
                )
                reviewed = original.reject(reviewer_id, reviewed_at, message)
                _set_review(record, reviewed)
                await session.flush()
                return reviewed
        except SQLAlchemyError as error:
            _raise_if_temporarily_unavailable(error)
            raise

    async def _apply(self, session: AsyncSession, plan: ApprovalPlan) -> MasterCode:
        if plan.proposal.operation is ChangeOperation.CREATE:
            timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(plan.audit)
            resolved: dict[str, MasterCodeDimension | None] = {}
            invalid: list[str] = []
            for slot, selection in plan.validated.dimensions:
                if isinstance(selection, NotApplicable):
                    resolved[slot] = None
                elif isinstance(selection, ExistingDimension):
                    resolved[slot] = await self._master_codes._lock_reference(
                        session, slot, selection.id
                    )
                    if resolved[slot] is None:
                        invalid.append(f"payload.dimensions.{slot}.id")
                else:
                    resolved[slot] = await self._master_codes._create_inline(
                        session, slot, selection, mutation_timestamp=timestamp
                    )
            if invalid:
                raise InvalidDimensionReference(tuple(invalid))
            dimensions = MasterCodeDimensions(**resolved)
            code = MasterCode.compose(dimensions)
            existing = await session.scalar(
                select(MasterCodeRecord).where(MasterCodeRecord.code == code).with_for_update()
            )
            if existing is not None and existing.deleted_at is None:
                raise ChangeRequestNoEffect
            record = MasterCodeRecord(
                **{
                    f"{slot}_id": None if dimension is None else dimension.id
                    for slot, dimension in resolved.items()
                },
                code=code,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(record)
            await session.flush()
            return _master_code(record, dimensions)

        record, current, next_dimensions, timestamp = await self._lock_target(session, plan)
        operation = plan.proposal.operation
        if operation is ChangeOperation.DELETE:
            if current.deleted_at is not None:
                raise ChangeRequestNoEffect
        elif operation is ChangeOperation.RESTORE:
            if current.deleted_at is None:
                raise MasterCodeNotDeleted
            if _dimension_ids(next_dimensions) != _dimension_ids(current.dimensions):
                raise ChangeRequestRestoreMismatch
            inactive = tuple(
                f"{slot}_id"
                for slot in MasterCodeDimensions.ORDER
                if (dimension := getattr(current.dimensions, slot)) is not None
                and dimension.is_deleted
            )
            if inactive:
                raise MasterCodeReferenceInactive(inactive)
        else:
            if current.deleted_at is not None:
                raise MasterCodeNotFound
            if _dimension_ids(next_dimensions) == _dimension_ids(current.dimensions):
                raise ChangeRequestNoEffect
        if timestamp is None:
            timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                plan.audit, minimum_timestamp=current.updated_at
            )
        if operation is ChangeOperation.DELETE:
            updated = current.delete(changed_at=timestamp)
        elif operation is ChangeOperation.RESTORE:
            updated = current.restore(changed_at=timestamp)
        else:
            updated = current.update_references(dimensions=next_dimensions, changed_at=timestamp)
            for slot in MasterCodeDimensions.ORDER:
                dimension = getattr(next_dimensions, slot)
                setattr(record, f"{slot}_id", None if dimension is None else dimension.id)
        record.code = updated.code
        record.version = updated.version
        record.updated_at = updated.updated_at
        record.deleted_at = updated.deleted_at
        await session.flush()
        return updated

    async def _lock_target(
        self, session: AsyncSession, plan: ApprovalPlan
    ) -> tuple[MasterCodeRecord, MasterCode, MasterCodeDimensions, datetime | None]:
        row = (
            await session.execute(
                _joined_statement().where(MasterCodeRecord.id == plan.proposal.target_id)
            )
        ).one_or_none()
        if row is None:
            raise MasterCodeNotFound
        initial = _joined_to_domain(row)
        changes = dict(plan.validated.dimensions)
        timestamp = None
        if any(isinstance(value, InlineDimension) for value in changes.values()):
            timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                plan.audit, minimum_timestamp=initial.updated_at
            )
        current_dimensions: dict[str, MasterCodeDimension | None] = {}
        resolved: dict[str, MasterCodeDimension | None] = {}
        invalid: list[str] = []
        for slot in MasterCodeDimensions.ORDER:
            current = getattr(initial.dimensions, slot)
            change = changes.get(slot)
            identifiers = set()
            if current is not None:
                identifiers.add(current.id)
            if isinstance(change, ExistingDimension):
                identifiers.add(change.id)
            config = _SLOTS[slot]
            locked: dict[UUID, MasterCodeDimension] = {}
            if identifiers:
                rows = (
                    await session.scalars(
                        select(config.record_type)
                        .where(config.record_type.id.in_(sorted(identifiers)))
                        .order_by(config.record_type.id)
                        .with_for_update(read=True)
                        .execution_options(populate_existing=True)
                    )
                ).all()
                locked = {item.id: config.to_domain(item) for item in rows}
            current_dimensions[slot] = None if current is None else locked.get(current.id)
            if current is not None and current_dimensions[slot] is None:
                raise ChangeRequestStaleTarget
            if change is None:
                resolved[slot] = current_dimensions[slot]
            elif isinstance(change, NotApplicable):
                resolved[slot] = None
            elif isinstance(change, ExistingDimension):
                dimension = locked.get(change.id)
                if dimension is None or (
                    dimension.is_deleted and plan.proposal.operation is not ChangeOperation.RESTORE
                ):
                    invalid.append(f"payload.dimensions.{slot}.id")
                resolved[slot] = dimension
            else:
                resolved[slot] = await self._master_codes._create_inline(
                    session, slot, change, mutation_timestamp=timestamp
                )
        if invalid:
            raise InvalidDimensionReference(tuple(invalid))
        record = await session.scalar(
            select(MasterCodeRecord)
            .where(MasterCodeRecord.id == initial.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None or not _same_persisted_state(record, initial):
            raise ChangeRequestStaleTarget
        current = _master_code(record, MasterCodeDimensions(**current_dimensions))
        if current.etag() != plan.proposal.expected_etag:
            raise ChangeRequestStaleTarget
        return record, current, MasterCodeDimensions(**resolved), timestamp


async def _lock_request(session: AsyncSession, request_id: UUID) -> MasterCodeChangeRequestRecord:
    record = await session.scalar(
        select(MasterCodeChangeRequestRecord)
        .where(MasterCodeChangeRequestRecord.id == request_id)
        .with_for_update()
    )
    if record is None:
        raise ChangeRequestNotFound
    _request(record).require_pending()
    return record


def _proposal_columns(proposal: ChangeProposal, prefix: str) -> dict[str, object]:
    return {
        f"{prefix}_operation": proposal.operation.value,
        f"{prefix}_target_id": proposal.target_id,
        f"{prefix}_payload": proposal.payload.to_dict() if proposal.payload is not None else None,
        f"{prefix}_expected_etag": proposal.expected_etag,
    }


def _proposal(record: MasterCodeChangeRequestRecord, prefix: str) -> ChangeProposal:
    payload = getattr(record, f"{prefix}_payload")
    return ChangeProposal(
        ChangeOperation(getattr(record, f"{prefix}_operation")),
        getattr(record, f"{prefix}_target_id"),
        ProposalPayload.from_dict(payload) if payload is not None else None,
        getattr(record, f"{prefix}_expected_etag"),
    )


def _request(record: MasterCodeChangeRequestRecord) -> ChangeRequest:
    return ChangeRequest(
        id=record.id,
        original=_proposal(record, "original"),
        requester_id=record.requester_id,
        created_at=record.created_at,
        reason=record.reason,
        status=ChangeRequestStatus(record.status),
        approved=_proposal(record, "approved") if record.approved_operation is not None else None,
        reviewer_id=record.reviewer_id,
        reviewed_at=record.reviewed_at,
        review_message=ReviewMessage(record.review_message)
        if record.review_message is not None
        else None,
        applied_change_set_id=record.applied_change_set_id,
    )


def _set_review(record: MasterCodeChangeRequestRecord, reviewed: ChangeRequest) -> None:
    record.status = reviewed.status.value
    record.reviewer_id = reviewed.reviewer_id
    record.reviewed_at = reviewed.reviewed_at
    record.review_message = reviewed.review_message.value if reviewed.review_message else None
    record.applied_change_set_id = reviewed.applied_change_set_id
    if reviewed.approved is not None:
        for name, value in _proposal_columns(reviewed.approved, "approved").items():
            setattr(record, name, value)
