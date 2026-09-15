import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.domain.auth import UserRole
from mdm.infrastructure.database import create_engine
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.settings import Settings
from mdm.main import create_app

ADMIN_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
USER_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")


def _write_key_files(directory: Path) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = directory / "jwt-private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "integration-key", "alg": "RS256", "use": "sig"})
    jwks_path = directory / "jwt-public-keys.json"
    jwks_path.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    return private_path, jwks_path


pytestmark = pytest.mark.integration
CASES = [
    ("company", "companies", "SAMSUNG"),
    ("brand", "brands", "GALAXY"),
    ("model", "models", "S24"),
    ("category", "categories", "PHONE"),
    ("country", "countries", "KOREA"),
    ("year", "years", 2026),
    ("network", "networks", 5),
    ("memory", "memories", {"amount": 1, "unit": "TB"}),
]


@pytest.fixture
async def lifecycle_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[AsyncClient, dict[str, str], AsyncEngine]]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE master_codes, master_code_logs, "
                + ", ".join(
                    f"dimension_{plural}, dimension_{singular}_logs"
                    for singular, plural, _ in CASES
                )
            )
        )
    private, public = _write_key_files(tmp_path)
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "integration-key")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(public))
    codec = build_access_jwt_codec(Settings())
    now = int(datetime.now(UTC).timestamp())
    tokens = {
        role.value: codec.issue(
            USER_ID if role == UserRole.USER else ADMIN_ID, role, issued_at=now
        ).reveal()
        for role in UserRole
    }
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            yield client, tokens, engine
    await engine.dispose()


def _headers(
    tokens: dict[str, str], role: str = "ADMIN", etag: str | None = None
) -> dict[str, str]:
    result = {"Authorization": f"Bearer {tokens[role]}"}
    if etag is not None:
        result["If-Match"] = etag
    return result


BASE = "/api/v1/master-codes"


async def _create(client, tokens, *, slots=None):
    dimensions = {
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
    dimensions.update(slots or {})
    response = await client.post(BASE, headers=_headers(tokens), json={"dimensions": dimensions})
    assert response.status_code == 201, response.text
    return response


async def _logs(engine, identifier):
    async with engine.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM master_code_logs WHERE master_code_id=:id "
                        "ORDER BY master_code_version"
                    ),
                    {"id": UUID(identifier)},
                )
            )
            .mappings()
            .all()
        )


async def test_lifecycle_end_to_end_preserves_state_visibility_etags_and_audit(lifecycle_client):
    client, tokens, engine = lifecycle_client
    created = await _create(
        client,
        tokens,
        slots={
            slot: {"mode": "CREATE", "code": f"A{i}", "value": value}
            for i, (slot, _, value) in enumerate(CASES)
        },
    )
    original = created.json()
    path = f"{BASE}/{original['id']}"
    headers = _headers(tokens)
    for action in ("tombstone", "restore"):
        response = (
            await client.get(f"{path}/{action}", headers=headers)
            if action == "tombstone"
            else await client.post(
                f"{path}/{action}", headers=_headers(tokens, etag=created.headers["etag"]), json={}
            )
        )
        assert response.status_code == 409
        assert response.json()["code"] == "MASTER_CODE_NOT_DELETED"
    deleted = await client.post(
        f"{path}/delete",
        headers=_headers(tokens, etag=created.headers["etag"]),
        json={"reason": "  사용 종료  "},
    )
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    for key in ("id", "code", "dimensions", "created_at"):
        assert body[key] == original[key]
    assert body["version"] == 2
    assert body["deleted_at"] == body["updated_at"]
    assert body["deleted_at"] is not None
    assert deleted.headers["etag"] != created.headers["etag"]
    tombstone = await client.get(f"{path}/tombstone", headers=headers)
    assert tombstone.json() == body
    assert tombstone.headers["etag"] == deleted.headers["etag"]
    assert (await client.get(path, headers=headers)).status_code == 404
    assert (await client.get(BASE, headers=headers)).json()["items"] == []
    assert (
        await client.patch(
            path,
            headers=_headers(tokens, etag=deleted.headers["etag"]),
            json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"{path}/delete", headers=_headers(tokens, etag=deleted.headers["etag"]), json={}
        )
    ).status_code == 404
    # Tombstones still reserve their reference combination and composed code.
    duplicate = await client.post(
        BASE,
        headers=headers,
        json={
            "dimensions": {
                slot: {"mode": "REFERENCE", "id": ref["id"]}
                for slot, ref in original["dimensions"].items()
            }
        },
    )
    assert duplicate.status_code == 409
    restored = await client.post(
        f"{path}/restore",
        headers=_headers(tokens, "SUPER_ADMIN", deleted.headers["etag"]),
        json={"reason": None},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3
    assert restored.json()["deleted_at"] is None
    assert restored.json()["dimensions"] == original["dimensions"]
    assert (await client.get(path, headers=headers)).json() == restored.json()
    logs = await _logs(engine, original["id"])
    assert [row["operation"] for row in logs] == ["CREATE", "DELETE", "RESTORE"]
    for row, response, _operation, role, reason, old_deleted, new_deleted in (
        (logs[1], deleted, "DELETE", "ADMIN", "사용 종료", False, True),
        (logs[2], restored, "RESTORE", "SUPER_ADMIN", None, True, False),
    ):
        assert row["actor_kind"] == "HUMAN"
        assert row["actor_id"] == str(ADMIN_ID)
        assert row["actor_role"] == role
        assert row["reason"] == reason
        assert row["master_code_version"] == response.json()["version"]
        assert row["changed_at"].isoformat() == response.json()["updated_at"].replace("Z", "+00:00")
        assert row["old_state"]["deleted"] is old_deleted
        assert row["new_state"]["deleted"] is new_deleted
        assert row["new_state"]["code"] == original["code"]
    assert len({row["change_set_id"] for row in logs}) == 3


@pytest.mark.parametrize(("slot", "plural", "value"), CASES)
async def test_deleted_references_block_restore_until_reactivated_and_tombstone_etag_is_current(
    lifecycle_client, slot, plural, value
):
    client, tokens, engine = lifecycle_client
    created = await _create(
        client, tokens, slots={slot: {"mode": "CREATE", "code": "A1", "value": value}}
    )
    path = f"{BASE}/{created.json()['id']}"
    dimension_path = f"/api/v1/dimensions/{plural}/{created.json()['dimensions'][slot]['id']}"
    deleted = await client.post(
        f"{path}/delete", json={}, headers=_headers(tokens, etag=created.headers["etag"])
    )
    assert deleted.status_code == 200, deleted.text
    dimension_deleted = await client.post(
        f"{dimension_path}/delete", json={}, headers=_headers(tokens, etag='"1"')
    )
    assert dimension_deleted.status_code == 200, dimension_deleted.text
    tombstone = await client.get(f"{path}/tombstone", headers=_headers(tokens))
    assert tombstone.status_code == 200, tombstone.text
    assert tombstone.headers["etag"] != deleted.headers["etag"]
    assert tombstone.json()["version"] == 2
    stale = await client.post(
        f"{path}/restore", json={}, headers=_headers(tokens, etag=deleted.headers["etag"])
    )
    assert stale.status_code == 412
    rejected = await client.post(
        f"{path}/restore", json={}, headers=_headers(tokens, etag=tombstone.headers["etag"])
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "MASTER_CODE_REFERENCE_INACTIVE"
    assert rejected.json()["violations"] == [
        {"field": f"{slot}_id", "message": "삭제된 Dimension 참조입니다."}
    ]
    assert len(await _logs(engine, created.json()["id"])) == 2
    restored_dimension = await client.post(
        f"{dimension_path}/restore",
        json={},
        headers=_headers(tokens, etag=dimension_deleted.headers["etag"]),
    )
    assert restored_dimension.status_code == 200
    changed = await client.patch(
        dimension_path,
        json={"code": "NEW"},
        headers=_headers(tokens, etag=restored_dimension.headers["etag"]),
    )
    assert changed.status_code == 200, changed.text
    current = await client.get(f"{path}/tombstone", headers=_headers(tokens))
    assert current.json()["version"] == 3  # RECOMPOSE also updates tombstones.
    restored = await client.post(
        f"{path}/restore", json={}, headers=_headers(tokens, etag=current.headers["etag"])
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 4
    assert restored.json()["code"] == current.json()["code"]
    assert restored.json()["dimensions"][slot]["code"] == "NEW"


@pytest.mark.parametrize("action", ["delete", "restore"])
async def test_lifecycle_input_preconditions_and_permissions_leave_no_changes(
    lifecycle_client, action
):
    client, tokens, engine = lifecycle_client
    created = await _create(client, tokens)
    path = f"{BASE}/{created.json()['id']}"
    current = created
    if action == "restore":
        current = await client.post(
            f"{path}/delete", json={}, headers=_headers(tokens, etag=created.headers["etag"])
        )
    etag = current.headers["etag"]
    url = f"{path}/{action}"
    expected_logs = await _logs(engine, created.json()["id"])
    for raw in (b"", b"null", b"[]", b"1", b'{"extra":true}', b'{"actor_kind":"SYSTEM"}'):
        response = await client.post(
            url,
            content=raw,
            headers={**_headers(tokens, etag=etag), "Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"
    for reason in (True, 1, "x" * 501, "a\nb", "a\tb"):
        response = await client.post(
            url, json={"reason": reason}, headers=_headers(tokens, etag=etag)
        )
        assert response.status_code == 422, response.text
    for bad, status, code in (
        (None, 428, "PRECONDITION_REQUIRED"),
        ("*", 400, "INVALID_IF_MATCH"),
        ("W/" + etag, 400, "INVALID_IF_MATCH"),
        (etag + "," + etag, 400, "INVALID_IF_MATCH"),
        ('"mc-1-' + "0" * 64 + '"', 412, "PRECONDITION_FAILED"),
    ):
        response = await client.post(url, json={}, headers=_headers(tokens, etag=bad))
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
    repeated = [*_headers(tokens).items(), ("If-Match", etag), ("If-Match", etag)]
    assert (await client.post(url, json={}, headers=repeated)).status_code == 400
    assert (await client.post(url, json={}, headers={"If-Match": etag})).status_code == 401
    assert (
        await client.post(url, json={}, headers=_headers(tokens, "USER", etag))
    ).status_code == 403
    assert (
        await client.get(f"{path}/tombstone", headers=_headers(tokens, "USER"))
    ).status_code == 403
    missing = await client.post(
        f"{BASE}/{uuid4()}/{action}", json={}, headers=_headers(tokens, etag=etag)
    )
    assert missing.status_code == 404
    assert await _logs(engine, created.json()["id"]) == expected_logs
    read_path = f"{path}/tombstone" if action == "restore" else path
    assert (await client.get(read_path, headers=_headers(tokens))).json() == current.json()


async def test_dimension_value_change_invalidates_delete_without_changing_master_version(
    lifecycle_client,
):
    client, tokens, engine = lifecycle_client
    created = await _create(
        client, tokens, slots={"company": {"mode": "CREATE", "code": "A1", "value": "A"}}
    )
    path = f"{BASE}/{created.json()['id']}"
    dimension_id = created.json()["dimensions"]["company"]["id"]
    changed = await client.patch(
        f"/api/v1/dimensions/companies/{dimension_id}",
        json={"value": "B"},
        headers=_headers(tokens, etag='"1"'),
    )
    assert changed.status_code == 200
    stale = await client.post(
        f"{path}/delete", json={}, headers=_headers(tokens, etag=created.headers["etag"])
    )
    assert stale.status_code == 412
    assert len(await _logs(engine, created.json()["id"])) == 1
    current = await client.get(path, headers=_headers(tokens))
    assert current.json()["version"] == 1
    assert current.headers["etag"] != created.headers["etag"]


@pytest.mark.parametrize("action", ["delete", "restore"])
async def test_concurrent_same_etag_mutations_commit_one_transition(lifecycle_client, action):
    client, tokens, engine = lifecycle_client
    current = await _create(client, tokens)
    identifier = current.json()["id"]
    path = f"{BASE}/{identifier}"
    if action == "restore":
        current = await client.post(
            f"{path}/delete", json={}, headers=_headers(tokens, etag=current.headers["etag"])
        )
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                client.post(
                    f"{path}/{action}",
                    json={},
                    headers=_headers(tokens, etag=current.headers["etag"]),
                )
                for _ in range(2)
            )
        ),
        timeout=10,
    )
    assert sum(response.status_code == 200 for response in results) == 1
    assert all(
        response.status_code in ({200, 404, 412} if action == "delete" else {200, 409, 412})
        for response in results
    )
    assert len(await _logs(engine, identifier)) == (2 if action == "delete" else 3)


@pytest.mark.parametrize("action", ["delete", "restore"])
async def test_audit_failure_rolls_back_lifecycle_and_physical_delete_and_log_mutation_are_denied(
    lifecycle_client, action
):
    client, tokens, engine = lifecycle_client
    current = await _create(client, tokens)
    identifier = current.json()["id"]
    path = f"{BASE}/{identifier}"
    if action == "restore":
        current = await client.post(
            f"{path}/delete", json={}, headers=_headers(tokens, etag=current.headers["etag"])
        )
    before = await _logs(engine, identifier)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION reject_lifecycle_test_log() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'test log failure'; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER reject_lifecycle_test_log BEFORE INSERT ON master_code_logs "
                "FOR EACH ROW EXECUTE FUNCTION reject_lifecycle_test_log()"
            )
        )
    try:
        failed = await client.post(
            f"{path}/{action}", json={}, headers=_headers(tokens, etag=current.headers["etag"])
        )
        assert failed.status_code == 500
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER reject_lifecycle_test_log ON master_code_logs"))
            await conn.execute(text("DROP FUNCTION reject_lifecycle_test_log()"))
    read_path = f"{path}/tombstone" if action == "restore" else path
    assert (await client.get(read_path, headers=_headers(tokens))).json() == current.json()
    assert await _logs(engine, identifier) == before
    for sql in (
        "DELETE FROM master_codes WHERE id=:id",
        "UPDATE master_code_logs SET reason='forged' WHERE master_code_id=:id",
        "DELETE FROM master_code_logs WHERE master_code_id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(text(sql), {"id": UUID(identifier)})


def _audit(*, dimension_operation=None, master_code_operation=None):
    from mdm.application.audit import HumanMutationAuditFactory
    from mdm.application.auth import HumanPrincipal
    from mdm.infrastructure.uuid7 import Uuid7Generator

    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(ADMIN_ID, UserRole.ADMIN),
        dimension_operation=dimension_operation,
        master_code_operation=master_code_operation,
        reason="동시성 검증",
    )


@pytest.mark.parametrize("action", ["restore", "reference_update"])
@pytest.mark.parametrize("master_first", [True, False])
async def test_master_mutation_and_dimension_deletion_serialize_in_both_orders(
    lifecycle_client, monkeypatch, action, master_first
):
    from mdm.application.dimension_lifecycle import DimensionInUse
    from mdm.application.master_codes import (
        ExistingDimension,
        InvalidDimensionReference,
        MasterCodeReferenceUpdate,
    )
    from mdm.application.preconditions import PreconditionFailed
    from mdm.domain.audit import DimensionOperation, MasterCodeOperation
    from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
    from mdm.infrastructure.database import create_session_factory
    from mdm.infrastructure.repositories.dimension_lifecycle import (
        SqlAlchemyCompanyLifecycleRepository,
    )
    from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository

    client, tokens, engine = lifecycle_client
    company = await client.post(
        "/api/v1/dimensions/companies", json={"code": "A1", "value": "A"}, headers=_headers(tokens)
    )
    dimension_id = UUID(company.json()["id"])
    slots = (
        {"company": {"mode": "REFERENCE", "id": str(dimension_id)}} if action == "restore" else {}
    )
    created = await _create(client, tokens, slots=slots)
    identifier = UUID(created.json()["id"])
    current = created
    if action == "restore":
        current = await client.post(
            f"{BASE}/{identifier}/delete",
            json={},
            headers=_headers(tokens, etag=created.headers["etag"]),
        )
    sessions = create_session_factory(engine)
    master = SqlAlchemyMasterCodeRepository(sessions)
    dimensions = SqlAlchemyCompanyLifecycleRepository(sessions)
    locked, release = asyncio.Event(), asyncio.Event()
    original = SqlAlchemyMutationAuditContextWriter.set_context
    operation = (
        MasterCodeOperation.RESTORE if action == "restore" else MasterCodeOperation.REFERENCE_UPDATE
    )

    async def hold_first(self, metadata, *, minimum_timestamp=None):
        is_first = (
            metadata.operations.master_code == operation
            if master_first
            else metadata.operations.dimension == DimensionOperation.DELETE
        )
        if is_first:
            locked.set()
            await release.wait()
        return await original(self, metadata, minimum_timestamp=minimum_timestamp)

    monkeypatch.setattr(SqlAlchemyMutationAuditContextWriter, "set_context", hold_first)

    async def mutate_master():
        if action == "restore":
            return await master.restore(
                identifier, current.headers["etag"], _audit(master_code_operation=operation)
            )
        return await master.update_references(
            identifier,
            MasterCodeReferenceUpdate(company=ExistingDimension(dimension_id)),
            current.headers["etag"],
            _audit(master_code_operation=operation),
        )

    async def delete_dimension():
        return await dimensions.delete(
            dimension_id, 1, _audit(dimension_operation=DimensionOperation.DELETE)
        )

    first = asyncio.create_task(mutate_master() if master_first else delete_dimension())
    second = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        second = asyncio.create_task(delete_dimension() if master_first else mutate_master())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.15)
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock'"
                    )
                )
                >= 1
            )
    finally:
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(first, *([second] if second else []), return_exceptions=True), 10
        )
    assert not isinstance(results[0], BaseException), results
    error = (
        DimensionInUse
        if master_first
        else (PreconditionFailed if action == "restore" else InvalidDimensionReference)
    )
    assert isinstance(results[1], error), results
    async with engine.connect() as conn:
        deleted_at = await conn.scalar(
            text("SELECT deleted_at FROM dimension_companies WHERE id=:id"), {"id": dimension_id}
        )
        assert (deleted_at is None) == master_first
        version = await conn.scalar(
            text("SELECT version FROM master_codes WHERE id=:id"), {"id": identifier}
        )
        assert version == current.json()["version"] + int(master_first)
    logs = await _logs(engine, str(identifier))
    assert len(logs) == version


async def test_tombstone_load_uses_one_join_snapshot(lifecycle_client):
    from sqlalchemy import event

    from mdm.infrastructure.database import create_session_factory
    from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository

    client, tokens, engine = lifecycle_client
    created = await _create(client, tokens)
    identifier = UUID(created.json()["id"])
    deleted = await client.post(
        f"{BASE}/{identifier}/delete",
        json={},
        headers=_headers(tokens, etag=created.headers["etag"]),
    )
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        state = await SqlAlchemyMasterCodeRepository(create_session_factory(engine)).get_tombstone(
            identifier
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert statements[0].count("LEFT OUTER JOIN") == 8
    assert state.etag() == deleted.headers["etag"]


@pytest.mark.parametrize("deleted", [False, True])
async def test_database_rejects_invalid_lifecycle_contexts_transitions_and_versions(
    lifecycle_client, deleted
):
    from mdm.domain.audit import MasterCodeOperation
    from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
    from mdm.infrastructure.database import create_session_factory

    client, tokens, engine = lifecycle_client
    current = await _create(client, tokens)
    identifier = UUID(current.json()["id"])
    if deleted:
        current = await client.post(
            f"{BASE}/{identifier}/delete",
            json={},
            headers=_headers(tokens, etag=current.headers["etag"]),
        )
    before = await _logs(engine, str(identifier))
    sessions = create_session_factory(engine)
    # Every failed direct write must preserve both business state and audit history.
    assignments = (
        "deleted_at=NULL",
        "deleted_at=:timestamp",
        "deleted_at=NULL, version=version+2",
        "deleted_at=:timestamp, version=version+2",
        "deleted_at=:timestamp, version=version+1, code='BAD-NNN-NNN-NNN-NNN-NNN-NNN-NNN'",
    )
    for operation in (
        None,
        MasterCodeOperation.DELETE,
        MasterCodeOperation.RESTORE,
        MasterCodeOperation.RECOMPOSE,
    ):
        for assignment in assignments:
            # Valid lifecycle transitions are covered by HTTP tests above.
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    timestamp = await session.scalar(text("SELECT statement_timestamp()"))
                    if operation is not None:
                        timestamp = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                            _audit(master_code_operation=operation)
                        )
                    await session.execute(
                        text(
                            f"UPDATE master_codes SET {assignment}, updated_at=:timestamp "
                            "WHERE id=:id"
                        ),
                        {"id": identifier, "timestamp": timestamp},
                    )
    assert await _logs(engine, str(identifier)) == before
