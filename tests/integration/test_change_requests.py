import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationPolicy
from mdm.application.change_requests import (
    ChangeRequestNoEffect,
    ChangeRequestNotFound,
    ChangeRequestStaleTarget,
    ChangeRequestUseCases,
)
from mdm.application.master_codes import InlineDimensionConflict, MasterCodeConflict
from mdm.domain.auth import UserRole
from mdm.domain.change_requests import (
    ChangeOperation,
    ChangeProposal,
    ChangeRequestAlreadyReviewed,
    ChangeRequestStatus,
    ProposalPayload,
    ReviewMessage,
)
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
from mdm.infrastructure.models import MasterCodeChangeRequestRecord
from mdm.infrastructure.repositories.change_requests import SqlAlchemyChangeRequestRepository
from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator
from mdm.main import create_app

pytestmark = pytest.mark.integration
USER = HumanPrincipal(UUID("00000000-0000-7000-8000-000000000001"), UserRole.USER)
ADMIN = HumanPrincipal(UUID("00000000-0000-7000-8000-000000000002"), UserRole.ADMIN)
SLOTS = ("company", "brand", "model", "category", "year", "memory", "network", "country")


@pytest.fixture
async def workflow() -> AsyncGenerator[tuple[ChangeRequestUseCases, AsyncEngine]]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE master_code_change_requests, master_codes, master_code_logs, "
                "dimension_companies, dimension_company_logs, "
                "dimension_brands, dimension_brand_logs, "
                "dimension_models, dimension_model_logs, "
                "dimension_categories, dimension_category_logs, "
                "dimension_years, dimension_year_logs, dimension_memories, dimension_memory_logs, "
                "dimension_networks, dimension_network_logs, "
                "dimension_countries, dimension_country_logs"
            )
        )
    service = ChangeRequestUseCases(
        SqlAlchemyChangeRequestRepository(create_session_factory(engine)),
        AuthorizationPolicy(),
        HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new),
    )
    yield service, engine
    await engine.dispose()


def create_proposal() -> ChangeProposal:
    return ChangeProposal(
        ChangeOperation.CREATE,
        None,
        ProposalPayload.from_dict(
            {"dimensions": {slot: {"mode": "NOT_APPLICABLE"} for slot in SLOTS}}
        ),
        None,
    )


async def test_submission_has_no_mastercode_or_audit_effect(workflow) -> None:
    service, engine = workflow
    request = await service.submit(USER, create_proposal(), reason=None)
    assert request.status is ChangeRequestStatus.PENDING
    assert (await service.get_own(USER, request.id)).original == request.original
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_codes")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 0


async def test_approval_atomically_links_request_to_generated_mastercode_log(workflow) -> None:
    service, engine = workflow
    request = await service.submit(USER, create_proposal(), reason=None)
    result = await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    assert result.status is ChangeRequestStatus.APPROVED
    async with engine.connect() as conn:
        logs = (
            await conn.execute(text("SELECT change_set_id, actor_id FROM master_code_logs"))
        ).all()
    assert logs == [(result.applied_change_set_id, str(ADMIN.user_id))]


async def _create_mastercode(service, engine):
    request = await service.submit(USER, create_proposal(), reason=None)
    reviewed = await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    async with engine.connect() as conn:
        target_id = await conn.scalar(
            text("SELECT master_code_id FROM master_code_logs WHERE change_set_id=:id"),
            {"id": reviewed.applied_change_set_id},
        )
    repository = SqlAlchemyMasterCodeRepository(create_session_factory(engine))
    return await repository.get_active(target_id)


async def test_no_effect_keeps_pending_until_explicit_rejection(workflow):
    service, engine = workflow
    await _create_mastercode(service, engine)
    request = await service.submit(USER, create_proposal(), reason=None)
    with pytest.raises(ChangeRequestNoEffect):
        await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    assert (await service.get_own(USER, request.id)).status is ChangeRequestStatus.PENDING
    rejected = await service.reject(
        ADMIN, request.id, message=ReviewMessage("이미 등록된 조합입니다.")
    )
    assert rejected.status is ChangeRequestStatus.REJECTED
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 1


async def test_only_one_concurrent_reviewer_can_finish_request(workflow):
    service, engine = workflow
    request = await service.submit(USER, create_proposal(), reason=None)
    results = await asyncio.gather(
        service.approve(ADMIN, request.id, approved_proposal=None, message=None),
        service.approve(ADMIN, request.id, approved_proposal=None, message=None),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ChangeRequestAlreadyReviewed) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 1


async def test_stale_target_approval_keeps_pending_and_latest_etag_requires_modified_review(
    workflow,
):
    service, engine = workflow
    target = await _create_mastercode(service, engine)
    stale = ChangeProposal(ChangeOperation.DELETE, target.id, None, '"mc-1-' + "0" * 64 + '"')
    request = await service.submit(USER, stale, reason=None)
    with pytest.raises(ChangeRequestStaleTarget):
        await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    assert (await service.get_own(USER, request.id)).status is ChangeRequestStatus.PENDING
    latest = ChangeProposal(ChangeOperation.DELETE, target.id, None, target.etag())
    result = await service.approve(
        ADMIN,
        request.id,
        approved_proposal=latest,
        message=ReviewMessage("현재 상태를 확인하고 삭제합니다."),
    )
    assert result.status is ChangeRequestStatus.MODIFIED_AND_APPROVED
    assert result.original == stale
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 2


async def test_other_requester_is_hidden_and_promoted_owner_can_still_read_and_review(workflow):
    service, _ = workflow
    request = await service.submit(USER, create_proposal(), reason=None)
    with pytest.raises(ChangeRequestNotFound):
        await service.get_own(ADMIN, request.id)
    promoted = HumanPrincipal(USER.user_id, UserRole.ADMIN)
    assert (await service.get_own(promoted, request.id)).id == request.id
    result = await service.approve(promoted, request.id, approved_proposal=None, message=None)
    assert result.reviewer_id == result.requester_id == USER.user_id


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE master_code_change_requests SET original_operation='DELETE' WHERE id=:id",
        "UPDATE master_code_change_requests SET reason='tampered' WHERE id=:id",
        "DELETE FROM master_code_change_requests WHERE id=:id",
    ],
)
async def test_db_refuses_request_mutation_or_deletion(workflow, sql):
    service, engine = workflow
    request = await service.submit(USER, create_proposal(), reason=None)
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text(sql), {"id": request.id})
    assert (await service.get_own(USER, request.id)).original == request.original


def _inline_proposal(**overrides):
    values = (
        "Company",
        "Brand",
        "Model",
        "Category",
        2026,
        {"amount": 128, "unit": "gb"},
        5,
        "Country",
    )
    dimensions = {
        slot: {"mode": "CREATE", "code": f"a{i}", "value": value}
        for i, (slot, value) in enumerate(zip(SLOTS, values, strict=True))
    }
    dimensions.update(overrides)
    return ChangeProposal(
        ChangeOperation.CREATE, None, ProposalPayload.from_dict({"dimensions": dimensions}), None
    )


async def test_inline_approval_uses_one_change_set_for_all_eight_dimensions(workflow):
    service, engine = workflow
    original = _inline_proposal()
    request = await service.submit(USER, original, reason="사용자 설명")
    result = await service.approve(
        ADMIN, request.id, approved_proposal=None, message=ReviewMessage("관리자 승인 이유")
    )
    assert result.original == original
    async with engine.connect() as conn:
        for slot in SLOTS:
            rows = (
                await conn.execute(
                    text(f"SELECT change_set_id, actor_id, reason FROM dimension_{slot}_logs")
                )
            ).all()
            assert len(rows) == 2
            assert all(
                row == (result.applied_change_set_id, str(ADMIN.user_id), "관리자 승인 이유")
                for row in rows
            )
        assert await conn.scalar(text("SELECT code FROM dimension_companies")) == "A0"
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 1


async def test_inline_conflict_rolls_back_earlier_creations_and_keeps_pending(workflow):
    service, engine = workflow
    first = await service.submit(USER, _inline_proposal(), reason=None)
    await service.approve(ADMIN, first.id, approved_proposal=None, message=None)
    proposal = _inline_proposal(company={"mode": "CREATE", "code": "NEW", "value": "NEW"})
    request = await service.submit(USER, proposal, reason=None)
    with pytest.raises(InlineDimensionConflict):
        await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    assert (await service.get_own(USER, request.id)).status is ChangeRequestStatus.PENDING
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM dimension_companies")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM dimension_company_logs")) == 2
        assert await conn.scalar(text("SELECT count(*) FROM master_codes")) == 1


async def test_review_persistence_failure_rolls_back_all_data_and_logs(workflow):
    service, engine = workflow
    request = await service.submit(USER, _inline_proposal(), reason=None)
    async with engine.begin() as conn:
        await conn.execute(
            text("""CREATE FUNCTION test_reject_approval_write() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$""")
        )
        await conn.execute(
            text("""CREATE TRIGGER test_reject_approval_write BEFORE UPDATE
            ON master_code_change_requests FOR EACH ROW
            EXECUTE FUNCTION test_reject_approval_write()""")
        )
    try:
        with pytest.raises(DBAPIError):
            await service.approve(ADMIN, request.id, approved_proposal=None, message=None)
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER test_reject_approval_write ON master_code_change_requests")
            )
            await conn.execute(text("DROP FUNCTION test_reject_approval_write()"))
    assert (await service.get_own(USER, request.id)).status is ChangeRequestStatus.PENDING
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_codes")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 0
        for slot in SLOTS:
            assert await conn.scalar(text(f"SELECT count(*) FROM dimension_{slot}_logs")) == 0


@pytest.fixture
async def api_workflow(workflow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, engine = workflow
    private, public = tmp_path / "private.pem", tmp_path / "public.json"
    generate_local_jwt_keys(kid="request-test", private_key_path=private, jwks_path=public)
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "request-test")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(public))
    codec = build_access_jwt_codec(Settings())
    now = int(datetime.now(UTC).timestamp())
    headers = {
        "user": {
            "Authorization": "Bearer "
            + codec.issue(USER.user_id, UserRole.USER, issued_at=now).reveal()
        },
        "admin": {
            "Authorization": "Bearer "
            + codec.issue(ADMIN.user_id, UserRole.ADMIN, issued_at=now).reveal()
        },
    }
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            yield client, headers, engine


BASE = "/api/v1/master-code-change-requests"
ADMIN_BASE = "/api/v1/admin/master-code-change-requests"


async def _submit_http(client, headers, *, proposal=None):
    inline = _inline_proposal().payload
    assert inline is not None
    original = proposal or {"operation": "CREATE", "payload": inline.to_dict()}
    response = await client.post(BASE, headers=headers["user"], json={"proposal": original})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_http_submit_review_and_owner_result_preserve_original(api_workflow):
    client, headers, engine = api_workflow
    identifier = await _submit_http(client, headers)
    queue = await client.get(ADMIN_BASE, headers=headers["admin"])
    assert queue.status_code == 200, queue.text
    assert [item["id"] for item in queue.json()["items"]] == [identifier]
    assert queue.json()["items"][0]["original"]["payload"]["dimensions"]["company"]["code"] == "a0"
    approved = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval", headers=headers["admin"], json={}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    result = await client.get(f"{BASE}/{identifier}", headers=headers["user"])
    assert result.json() == approved.json()
    assert result.json()["original"]["payload"]["dimensions"]["company"]["code"] == "a0"
    assert (await client.get(ADMIN_BASE, headers=headers["admin"])).json()["items"] == []
    duplicate_review = await client.post(
        f"{ADMIN_BASE}/{identifier}/rejection",
        headers=headers["admin"],
        json={"review_message": "재검토"},
    )
    assert duplicate_review.status_code == 409
    assert duplicate_review.json()["code"] == "CHANGE_REQUEST_ALREADY_REVIEWED"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT code FROM dimension_companies")) == "A0"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("  신규  제품 등록  ", "신규  제품 등록"),
        ("   ", None),
        (" " * 1000 + "가" * 500 + " " * 1000, "가" * 500),
    ],
    ids=["trim-edges-preserve-content", "spaces-only", "padded-length-boundary"],
)
async def test_http_reason_is_normalized_in_storage_and_queries(api_workflow, reason, expected):
    client, headers, engine = api_workflow
    payload = _inline_proposal().payload
    assert payload is not None
    original = {"operation": "CREATE", "payload": payload.to_dict()}
    submitted = await client.post(
        BASE, headers=headers["user"], json={"proposal": original, "reason": reason}
    )
    assert submitted.status_code == 201, submitted.text
    identifier = submitted.json()["id"]

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT reason FROM master_code_change_requests WHERE id=:id"),
                {"id": UUID(identifier)},
            )
        ).one()
        assert stored.reason == expected
    for base, actor in ((BASE, "user"), (ADMIN_BASE, "admin")):
        detail = await client.get(f"{base}/{identifier}", headers=headers[actor])
        assert detail.status_code == 200, detail.text
        assert detail.json()["reason"] == expected
        assert detail.json()["original"]["payload"] == original["payload"]
        listing = await client.get(base, headers=headers[actor])
        assert listing.status_code == 200, listing.text
        assert [(item["id"], item["reason"]) for item in listing.json()["items"]] == [
            (identifier, expected)
        ]


async def test_http_authorization_and_visibility(api_workflow):
    client, headers, _ = api_workflow
    identifier = await _submit_http(client, headers)
    assert (await client.get(BASE)).status_code == 401
    assert (await client.get(ADMIN_BASE, headers=headers["user"])).status_code == 403
    assert (await client.get(f"{BASE}/{identifier}", headers=headers["admin"])).status_code == 404
    for suffix in ("approval", "rejection"):
        result = await client.post(
            f"{ADMIN_BASE}/{identifier}/{suffix}",
            headers=headers["user"],
            json={"review_message": "확인"},
        )
        assert result.status_code == 403
    for endpoint in (BASE, ADMIN_BASE):
        result = await client.get(f"{endpoint}/{ADMIN.user_id}", headers=headers["admin"])
        assert result.json()["code"] == "CHANGE_REQUEST_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"review_message": None},
        {"review_message": ""},
        {"review_message": "  "},
        {"review_message": "줄\n바꿈"},
        {"approved_proposal": None},
        {"extra": True},
    ],
)
async def test_http_invalid_approval_body_returns_problem_without_review(api_workflow, payload):
    client, headers, _ = api_workflow
    identifier = await _submit_http(client, headers)
    response = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval", headers=headers["admin"], json=payload
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert (await client.get(f"{BASE}/{identifier}", headers=headers["user"])).json()[
        "status"
    ] == "PENDING"


async def test_http_partial_inline_update_preserves_omitted_slots(api_workflow):
    client, headers, _ = api_workflow
    payload = create_proposal().payload
    assert payload is not None
    initial = await client.post(
        "/api/v1/master-codes", headers=headers["admin"], json=payload.to_dict()
    )
    assert initial.status_code == 201, initial.text
    dimensions = {"memory": {"mode": "CREATE", "code": "mem", "value": {"amount": 1, "unit": "tb"}}}
    proposal = {
        "operation": "REFERENCE_UPDATE",
        "target_id": initial.json()["id"],
        "expected_etag": initial.headers["etag"],
        "payload": {"dimensions": dimensions},
    }
    identifier = await _submit_http(client, headers, proposal=proposal)
    detail = await client.get(f"{BASE}/{identifier}", headers=headers["user"])
    assert detail.json()["original"]["payload"]["dimensions"] == dimensions
    approved = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval", headers=headers["admin"], json={}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["original"]["payload"]["dimensions"] == dimensions
    current = await client.get(
        f"/api/v1/master-codes/{initial.json()['id']}", headers=headers["user"]
    )
    assert current.json()["version"] == 2
    assert current.json()["dimensions"]["memory"]["value"]["capacity_mb"] == 1000000


async def test_http_create_to_restore_retains_original_and_records_actual_restore(api_workflow):
    client, headers, engine = api_workflow
    payload = create_proposal().payload
    assert payload is not None
    initial = await client.post(
        "/api/v1/master-codes", headers=headers["admin"], json=payload.to_dict()
    )
    target_id = initial.json()["id"]
    deleted = await client.request(
        "POST",
        f"/api/v1/master-codes/{target_id}/delete",
        headers={**headers["admin"], "If-Match": initial.headers["etag"]},
        json={},
    )
    assert deleted.status_code == 200, deleted.text
    identifier = await _submit_http(
        client, headers, proposal={"operation": "CREATE", "payload": payload.to_dict()}
    )
    collision = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval", headers=headers["admin"], json={}
    )
    assert collision.status_code == 409
    assert collision.json()["code"] == "MASTER_CODE_CONFLICT"
    approved = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval",
        headers=headers["admin"],
        json={
            "approved_proposal": {
                "operation": "RESTORE",
                "target_id": target_id,
                "expected_etag": deleted.headers["etag"],
                "payload": payload.to_dict(),
            },
            "review_message": "신규 생성 대신 기존 조합을 복원했습니다.",
        },
    )
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["status"] == "MODIFIED_AND_APPROVED"
    assert result["original"]["operation"] == "CREATE"
    assert result["original"]["target_id"] is None
    assert result["approved_proposal"]["target_id"] == target_id
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT operation FROM master_code_logs WHERE change_set_id=:id"),
                {"id": UUID(result["applied_change_set_id"])},
            )
            == "RESTORE"
        )


async def test_http_no_effect_and_stale_target_have_their_approved_codes(api_workflow):
    client, headers, _ = api_workflow
    payload = create_proposal().payload
    assert payload is not None
    initial = await client.post(
        "/api/v1/master-codes", headers=headers["admin"], json=payload.to_dict()
    )
    identifier = await _submit_http(
        client, headers, proposal={"operation": "CREATE", "payload": payload.to_dict()}
    )
    no_effect = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval", headers=headers["admin"], json={}
    )
    assert no_effect.status_code == 409
    assert no_effect.json()["code"] == "CHANGE_REQUEST_NO_EFFECT"
    stale_id = await _submit_http(
        client,
        headers,
        proposal={
            "operation": "DELETE",
            "target_id": initial.json()["id"],
            "expected_etag": '"mc-1-' + "0" * 64 + '"',
        },
    )
    stale = await client.post(
        f"{ADMIN_BASE}/{stale_id}/approval", headers=headers["admin"], json={}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CHANGE_REQUEST_STALE_TARGET"
    for pending_id in (identifier, stale_id):
        assert (await client.get(f"{BASE}/{pending_id}", headers=headers["user"])).json()[
            "status"
        ] == "PENDING"


async def test_http_equal_timestamp_cursor_and_filters_have_no_duplicates_or_omissions(
    api_workflow,
):
    client, headers, engine = api_workflow
    payload = create_proposal().payload
    assert payload is not None
    ids = [UUID(f"00000000-0000-7000-8000-{index:012d}") for index in range(10, 16)]
    async with create_session_factory(engine).begin() as session:
        session.add_all(
            MasterCodeChangeRequestRecord(
                id=identifier,
                original_operation="CREATE",
                original_payload=payload.to_dict(),
                requester_id=USER.user_id if index < 5 else ADMIN.user_id,
                created_at=datetime(2026, 9, 15, tzinfo=UTC),
            )
            for index, identifier in enumerate(ids)
        )
    found = []
    query = {"limit": "2", "operation": "CREATE", "status": "PENDING"}
    while True:
        response = await client.get(BASE, headers=headers["user"], params=query)
        assert response.status_code == 200, response.text
        found.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
        query["cursor"] = cursor
    assert found == [str(identifier) for identifier in reversed(ids[:5])]
    assert (await client.get(BASE, headers=headers["user"], params={"operation": "DELETE"})).json()[
        "items"
    ] == []
    rejected = await client.post(
        f"{ADMIN_BASE}/{ids[0]}/rejection",
        headers=headers["admin"],
        json={"review_message": "이미 처리했습니다."},
    )
    assert rejected.status_code == 200
    own = await client.get(BASE, headers=headers["user"], params={"status": "REJECTED"})
    assert [item["id"] for item in own.json()["items"]] == [str(ids[0])]
    admin = await client.get(ADMIN_BASE, headers=headers["admin"])
    assert len(admin.json()["items"]) == 5


async def test_http_same_normalized_text_still_counts_as_modified_proposal(api_workflow):
    client, headers, _ = api_workflow
    raw = _inline_proposal().payload
    assert raw is not None
    proposal = {"operation": "CREATE", "payload": raw.to_dict()}
    identifier = await _submit_http(client, headers, proposal=proposal)
    modified_payload = raw.to_dict()
    dimensions = modified_payload["dimensions"]
    assert isinstance(dimensions, dict)
    dimensions["company"]["code"] = "A0"
    proposal["payload"] = modified_payload
    rejected = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval",
        headers=headers["admin"],
        json={"approved_proposal": proposal},
    )
    assert rejected.status_code == 422
    approved = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval",
        headers=headers["admin"],
        json={"approved_proposal": proposal, "review_message": "대문자로 수정해 승인합니다."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "MODIFIED_AND_APPROVED"
    assert approved.json()["original"]["payload"]["dimensions"]["company"]["code"] == "a0"


async def test_http_restore_rejects_mismatched_and_inactive_references(api_workflow):
    client, headers, _ = api_workflow
    payload = _inline_proposal().payload
    assert payload is not None
    initial = await client.post(
        "/api/v1/master-codes", headers=headers["admin"], json=payload.to_dict()
    )
    target = initial.json()
    target_id = target["id"]
    deleted = await client.post(
        f"/api/v1/master-codes/{target_id}/delete",
        headers={**headers["admin"], "If-Match": initial.headers["etag"]},
        json={},
    )
    assert deleted.status_code == 200
    empty = create_proposal().payload
    assert empty is not None
    identifier = await _submit_http(
        client, headers, proposal={"operation": "CREATE", "payload": empty.to_dict()}
    )
    mismatch = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval",
        headers=headers["admin"],
        json={
            "approved_proposal": {
                "operation": "RESTORE",
                "target_id": target_id,
                "expected_etag": deleted.headers["etag"],
                "payload": empty.to_dict(),
            },
            "review_message": "삭제 행 복원",
        },
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["code"] == "CHANGE_REQUEST_RESTORE_MISMATCH"
    company_id = target["dimensions"]["company"]["id"]
    company_path = f"/api/v1/dimensions/companies/{company_id}"
    company = await client.get(company_path, headers=headers["admin"])
    deleted_company = await client.post(
        f"{company_path}/delete",
        headers={**headers["admin"], "If-Match": company.headers["etag"]},
        json={},
    )
    assert deleted_company.status_code == 200, deleted_company.text
    tombstone = await client.get(
        f"/api/v1/master-codes/{target_id}/tombstone", headers=headers["admin"]
    )
    references = {
        slot: {"mode": "REFERENCE", "id": target["dimensions"][slot]["id"]} for slot in SLOTS
    }
    inactive = await client.post(
        f"{ADMIN_BASE}/{identifier}/approval",
        headers=headers["admin"],
        json={
            "approved_proposal": {
                "operation": "RESTORE",
                "target_id": target_id,
                "expected_etag": tombstone.headers["etag"],
                "payload": {"dimensions": references},
            },
            "review_message": "삭제 행 복원",
        },
    )
    assert inactive.status_code == 409, inactive.text
    assert inactive.json()["code"] == "MASTER_CODE_REFERENCE_INACTIVE"
    assert (await client.get(f"{BASE}/{identifier}", headers=headers["user"])).json()[
        "status"
    ] == "PENDING"


@pytest.mark.parametrize("inline", [False, True])
async def test_competing_requests_leave_losing_proposal_pending(workflow, inline):
    service, engine = workflow
    proposal = _inline_proposal() if inline else create_proposal()
    requests = [await service.submit(USER, proposal, reason=None) for _ in range(2)]
    results = await asyncio.gather(
        *(
            service.approve(ADMIN, request.id, approved_proposal=None, message=None)
            for request in requests
        ),
        return_exceptions=True,
    )
    assert (
        sum(
            isinstance(result, (InlineDimensionConflict, MasterCodeConflict, ChangeRequestNoEffect))
            for result in results
        )
        == 1
    )
    statuses = [(await service.get_own(USER, request.id)).status for request in requests]
    assert sorted(statuses) == sorted([ChangeRequestStatus.PENDING, ChangeRequestStatus.APPROVED])
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_codes")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM master_code_logs")) == 1


async def test_reference_approval_racing_dimension_deletion_commits_only_a_valid_outcome(
    api_workflow,
):
    client, headers, _ = api_workflow
    company = await client.post(
        "/api/v1/dimensions/companies",
        headers=headers["admin"],
        json={"code": "COM", "value": "COMPANY"},
    )
    assert company.status_code == 201, company.text
    empty = create_proposal().payload
    assert empty is not None
    master = await client.post(
        "/api/v1/master-codes", headers=headers["admin"], json=empty.to_dict()
    )
    assert master.status_code == 201
    request_id = await _submit_http(
        client,
        headers,
        proposal={
            "operation": "REFERENCE_UPDATE",
            "target_id": master.json()["id"],
            "expected_etag": master.headers["etag"],
            "payload": {
                "dimensions": {"company": {"mode": "REFERENCE", "id": company.json()["id"]}}
            },
        },
    )
    approved, deleted = await asyncio.gather(
        client.post(f"{ADMIN_BASE}/{request_id}/approval", headers=headers["admin"], json={}),
        client.post(
            f"/api/v1/dimensions/companies/{company.json()['id']}/delete",
            headers={**headers["admin"], "If-Match": company.headers["etag"]},
            json={},
        ),
    )
    assert (approved.status_code, deleted.status_code) in {(200, 409), (422, 200)}, (
        approved.text,
        deleted.text,
    )
    if approved.status_code == 200:
        assert deleted.json()["code"] == "DIMENSION_IN_USE"
    else:
        assert approved.json()["code"] == "INVALID_DIMENSION_REFERENCE"
