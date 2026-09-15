from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mdm.api.audit_logs import build_audit_log_router
from mdm.api.errors import dimension_validation_error_handler, validation_error_handler
from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.audit_logs import (
    AuditLogCursor,
    AuditLogFilters,
    AuditLogQuery,
    AuditSource,
    ListAuditLogs,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationPolicy
from mdm.application.change_requests import ChangeRequestUseCases
from mdm.domain.audit import ActorKind, DimensionOperation
from mdm.domain.auth import UserRole
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ProposalPayload,
    ReviewMessage,
)
from mdm.domain.dimensions import DimensionValidationError
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.audit_logs import SqlAlchemyAuditLogRepository
from mdm.infrastructure.repositories.change_requests import SqlAlchemyChangeRequestRepository
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator

pytestmark = pytest.mark.integration
ADMIN = HumanPrincipal(UUID(int=2), UserRole.ADMIN)
USER = HumanPrincipal(UUID(int=1), UserRole.USER)
SLOTS = ("company", "brand", "model", "category", "year", "memory", "network", "country")
COLLECTIONS = (
    "companies",
    "brands",
    "models",
    "categories",
    "years",
    "memories",
    "networks",
    "countries",
)


@pytest.fixture
async def audit_db():
    engine = create_engine(Settings().reveal_database_url())
    tables = ["master_code_change_requests", "master_codes", "master_code_logs"]
    for collection, slot in zip(COLLECTIONS, SLOTS, strict=True):
        tables += [f"dimension_{collection}", f"dimension_{slot}_logs"]
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE " + ", ".join(tables)))
    factory = create_session_factory(engine)
    service = ChangeRequestUseCases(
        SqlAlchemyChangeRequestRepository(factory),
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new),
    )
    values = (
        "Company",
        "Brand",
        "Model",
        "Category",
        2026,
        {"amount": 128, "unit": "GB"},
        5,
        "Country",
    )
    proposal = ChangeProposal(
        ChangeOperation.CREATE,
        None,
        ProposalPayload.from_dict(
            {
                "dimensions": {
                    slot: {"mode": "CREATE", "code": f"A{i}", "value": value}
                    for i, (slot, value) in enumerate(zip(SLOTS, values, strict=True))
                }
            }
        ),
        None,
    )
    request = await service.submit(USER, proposal, reason="원본 요청 설명")
    reviewed = await service.approve(
        ADMIN, request.id, approved_proposal=None, message=ReviewMessage("승인 사유")
    )
    repository = SqlAlchemyAuditLogRepository(factory)
    app = FastAPI()
    app.include_router(
        build_audit_log_router(
            use_case=ListAuditLogs(repository, AuthorizationPolicy()),
            principal_dependency=lambda: ADMIN,
            authorization=AuthorizationPolicy(),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(DimensionValidationError, dimension_validation_error_handler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield repository, engine, reviewed.applied_change_set_id, client
    await engine.dispose()


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 16, 17, 50])
async def test_approved_change_set_pages_include_every_typed_log_once(audit_db, limit):
    _, _, change_set_id, client = audit_db
    path = f"/api/v1/admin/change-sets/{change_set_id}/logs"
    cursor = None
    items = []
    while True:
        params = {"limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        items.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
        assert len(items) < 18
    assert len(items) == 17
    identities = [(item["source_kind"], item["source_type"], item["id"]) for item in items]
    assert len(set(identities)) == 17
    assert identities == sorted(identities, key=lambda key: (key[0], key[1], -UUID(key[2]).int))
    for item in items:
        assert item["change_set_id"] == str(change_set_id)
        assert item["actor_id"] == str(ADMIN.user_id)
        assert item["actor_role"] == "ADMIN"
        assert item["reason"] == "승인 사유"
        if item["source_kind"] == "MASTER_CODE":
            assert item["old_state"] is None
            assert item["new_state"]["deleted"] is False
            assert all(item["new_state"][f"{slot}_id"] is not None for slot in SLOTS)
        elif item["field_name"] == "VALUE":
            assert item["old_value"] is None
            if item["source_type"] in {"YEAR", "NETWORK"}:
                assert type(item["new_value"]) is int
            elif item["source_type"] == "MEMORY":
                assert item["new_value"] == {"amount": 128, "unit": "GB", "capacity_mb": 128000}
            else:
                assert type(item["new_value"]) is str


@pytest.mark.parametrize(
    "source,collection",
    [
        (AuditSource(slot.upper()), collection)
        for slot, collection in zip(SLOTS, COLLECTIONS, strict=True)
    ],
)
async def test_dimension_filter_combinations_and_time_boundaries(audit_db, source, collection):
    repository, _, change_set_id, client = audit_db
    all_logs = await repository.list_logs(AuditLogQuery(source), after=None, limit=50)
    item = all_logs.items[0]
    query = AuditLogQuery(
        source,
        change_set_id,
        AuditLogFilters(
            entity_id=item.dimension_id,
            changed_from=item.changed_at,
            changed_before=item.changed_at + timedelta(microseconds=1),
            actor_id=item.actor_id,
            actor_kind=ActorKind.HUMAN,
            actor_role=UserRole.ADMIN,
            operation=DimensionOperation.CREATE,
            field_name="VALUE",
        ),
    )
    page = await repository.list_logs(query, after=None, limit=50)
    assert len(page.items) == 1
    assert page.items[0].field_name == "VALUE"
    misses = [
        {"entity_id": UUID(int=999)},
        {"actor_id": str(USER.user_id)},
        {"actor_role": UserRole.USER},
        {"actor_kind": ActorKind.SYSTEM},
        {"operation": DimensionOperation.DELETE},
        {"field_name": "DELETED"},
        {"changed_from": item.changed_at + timedelta(microseconds=1), "changed_before": None},
        {"changed_from": None, "changed_before": item.changed_at},
    ]
    for miss in misses:
        absent = replace(query, filters=replace(query.filters, **miss))
        assert not (await repository.list_logs(absent, after=None, limit=50)).items
    response = await client.get(
        f"/api/v1/admin/dimension-logs/{collection}",
        params={
            "dimension_id": str(item.dimension_id),
            "field_name": "VALUE",
            "change_set_id": str(change_set_id),
            "actor_role": "ADMIN",
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_missing_identifiers_return_empty_pages(audit_db):
    _, _, _, client = audit_db
    for path, params in [
        (f"/api/v1/admin/change-sets/{UUID(int=999)}/logs", {}),
        ("/api/v1/admin/master-code-logs", {"master_code_id": str(UUID(int=999))}),
    ]:
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "next_cursor": None}


async def test_same_log_uuid_in_different_tables_remains_distinct_across_page_boundary(audit_db):
    repository, engine, _, _ = audit_db
    change_set = Uuid7Generator().new()
    common_id = Uuid7Generator().new()
    async with engine.begin() as connection:
        for slot in SLOTS:
            await connection.execute(
                text(f"""
                INSERT INTO dimension_{slot}_logs
                    (id, dimension_id, change_set_id, dimension_version, operation, field_name,
                     old_value, new_value, reason, actor_kind, actor_id, actor_role, changed_at)
                SELECT :id, dimension_id, :change_set, dimension_version, operation, field_name,
                       old_value, new_value, reason, actor_kind, actor_id, actor_role, changed_at
                FROM dimension_{slot}_logs WHERE field_name = 'CODE'
            """),
                {"id": common_id, "change_set": change_set},
            )
    query = AuditLogQuery(change_set_id=change_set)
    entries = []
    after = None
    while True:
        page = await repository.list_logs(query, after=after, limit=1)
        entries.extend(page.items)
        if not page.has_more:
            break
        last = page.items[-1]
        after = AuditLogCursor(last.changed_at, last.source_type, last.id)
        assert len(entries) < 9
    assert len(entries) == 8
    assert all(entry.id == common_id for entry in entries)
    assert [entry.source_type for entry in entries] == sorted(
        AuditSource(slot.upper()) for slot in SLOTS
    )


async def test_late_commit_before_cursor_is_visible_on_restart_without_losing_existing_rows(
    audit_db,
):
    from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
    from mdm.infrastructure.models import CompanyRecord

    repository, engine, _, client = audit_db
    query = AuditLogQuery(AuditSource.COMPANY)
    initial = await repository.list_logs(query, after=None, limit=50)
    factory = create_session_factory(engine)
    audit = HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        ADMIN,
        dimension_operation=DimensionOperation.CREATE,
    )
    async with factory.begin() as writer:
        await SqlAlchemyMutationAuditContextWriter(writer).set_context(audit)
        writer.add(CompanyRecord(code="LATE", value="LATE_COMMIT"))
        await writer.flush()
        first = await client.get("/api/v1/admin/dimension-logs/companies", params={"limit": 1})
        assert first.status_code == 200
        assert len((await repository.list_logs(query, after=None, limit=50)).items) == 2
        invisible = await client.get(f"/api/v1/admin/change-sets/{audit.change_set_id}/logs")
        assert invisible.json() == {"items": [], "next_cursor": None}
    second = await client.get(
        "/api/v1/admin/dimension-logs/companies",
        params={
            "cursor": first.json()["next_cursor"],
            "limit": 50,
        },
    )
    observed = first.json()["items"] + second.json()["items"]
    assert {row["id"] for row in observed} == {str(row.id) for row in initial.items}
    restart = await repository.list_logs(query, after=None, limit=50)
    assert len(restart.items) == 4
    visible = await client.get(f"/api/v1/admin/change-sets/{audit.change_set_id}/logs")
    assert len(visible.json()["items"]) == 2


async def test_mastercode_lifecycle_filters_preserve_historical_states(audit_db):
    from mdm.domain.audit import MasterCodeOperation
    from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository

    repository, engine, _, client = audit_db
    original_log = (
        await repository.list_logs(AuditLogQuery(AuditSource.MASTER_CODE), after=None, limit=50)
    ).items[0]
    master_repository = SqlAlchemyMasterCodeRepository(create_session_factory(engine))
    current = await master_repository.get_active(original_log.master_code_id)
    factory = HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new)
    deleted = await master_repository.delete(
        current.id,
        current.etag(),
        factory.create(
            ADMIN,
            master_code_operation=MasterCodeOperation.DELETE,
        ),
    )
    await master_repository.restore(
        deleted.id,
        deleted.etag(),
        factory.create(
            ADMIN,
            master_code_operation=MasterCodeOperation.RESTORE,
        ),
    )
    for operation, before, after in (("DELETE", False, True), ("RESTORE", True, False)):
        response = await client.get(
            "/api/v1/admin/master-code-logs",
            params={
                "master_code_id": str(current.id),
                "operation": operation,
                "actor_id": str(ADMIN.user_id),
                "actor_role": "ADMIN",
                "actor_kind": "HUMAN",
            },
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        item = response.json()["items"][0]
        assert item["old_state"]["deleted"] is before
        assert item["new_state"]["deleted"] is after
        assert item["old_state"]["code"] == item["new_state"]["code"] == current.code
        mismatch = await client.get("/api/v1/admin/master-code-logs", params={"actor_role": "USER"})
        assert mismatch.json() == {"items": [], "next_cursor": None}
    first = await client.get("/api/v1/admin/master-code-logs", params={"limit": 1})
    next_page = await client.get(
        "/api/v1/admin/master-code-logs", params={"cursor": first.json()["next_cursor"]}
    )
    assert [row["operation"] for row in first.json()["items"] + next_page.json()["items"]] == [
        "RESTORE",
        "DELETE",
        "CREATE",
    ]


async def test_real_asyncpg_query_cancellation_becomes_temporary_unavailability(audit_db):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from mdm.application.audit_logs import AuditLogRepositoryUnavailable

    _, engine, change_set_id, _ = audit_db
    async with engine.connect() as connection:
        await connection.execute(text("SET statement_timeout = '10ms'"))
        await connection.commit()

        def slow_select(conn, cursor, statement, parameters, context, executemany):
            return "SELECT pg_sleep(0.1)", ()

        event.listen(connection.sync_connection, "before_cursor_execute", slow_select, retval=True)
        try:
            repository = SqlAlchemyAuditLogRepository(async_sessionmaker(connection))
            with pytest.raises(AuditLogRepositoryUnavailable):
                await repository.list_logs(
                    AuditLogQuery(change_set_id=change_set_id), after=None, limit=50
                )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", slow_select)
            await connection.rollback()
            await connection.execute(text("SET statement_timeout = 0"))
            await connection.commit()
