from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.test_company_api_persistence import ADMIN_ID, USER_ID, _write_key_files

from mdm.domain.auth import UserRole
from mdm.infrastructure.database import create_engine
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.settings import Settings
from mdm.main import create_app

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


@pytest.mark.parametrize(("singular", "plural", "value"), CASES)
async def test_lifecycle_api_preserves_values_filters_deleted_rows_and_audits_once(
    lifecycle_client,
    singular: str,
    plural: str,
    value: Any,
) -> None:
    client, tokens, engine = lifecycle_client
    base = f"/api/v1/dimensions/{plural}"
    headers = _headers(tokens)
    created = await client.post(base, json={"code": "A1", "value": value}, headers=headers)
    assert created.status_code == 201, created.text
    original = created.json()
    path = f"{base}/{original['id']}"
    for suffix in ("tombstone", "restore"):
        response = (
            await client.get(f"{path}/{suffix}", headers=headers)
            if suffix == "tombstone"
            else await client.post(
                f"{path}/{suffix}", json={}, headers=_headers(tokens, etag='"1"')
            )
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "DIMENSION_NOT_DELETED"
    deleted = await client.post(
        f"{path}/delete", json={"reason": "  삭제 사유  "}, headers=_headers(tokens, etag='"1"')
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.headers["etag"] == '"2"'
    assert deleted.json()["deleted_at"] == deleted.json()["updated_at"]
    assert deleted.json()["deleted_at"] is not None
    tombstone = await client.get(f"{path}/tombstone", headers=headers)
    assert tombstone.json() == deleted.json()
    assert tombstone.headers["etag"] == '"2"'
    for field in ("id", "code", "value", "created_at"):
        assert deleted.json()[field] == original[field]
    hidden = await client.get(path, headers=headers)
    assert hidden.status_code == 404
    assert (await client.get(base, headers=headers)).json()["items"] == []
    for action in ("patch", "delete"):
        response = (
            await client.patch(path, json={"code": "B1"}, headers=_headers(tokens, etag='"2"'))
            if action == "patch"
            else await client.post(f"{path}/delete", json={}, headers=_headers(tokens, etag='"2"'))
        )
        assert response.status_code == 404
    duplicate = await client.post(base, json={"code": "A1", "value": value}, headers=headers)
    assert duplicate.status_code == 409
    stale = await client.post(f"{path}/restore", json={}, headers=_headers(tokens, etag='"1"'))
    assert stale.status_code == 412
    restored = await client.post(
        f"{path}/restore", json={"reason": None}, headers=_headers(tokens, "SUPER_ADMIN", '"2"')
    )
    assert restored.status_code == 200, restored.text
    assert restored.headers["etag"] == '"3"'
    assert restored.json()["deleted_at"] is None
    for field in ("id", "code", "value", "created_at"):
        assert restored.json()[field] == original[field]
    assert (await client.get(path, headers=headers)).json() == restored.json()
    async with engine.connect() as conn:
        logs = (
            (
                await conn.execute(
                    text(
                        f"SELECT * FROM dimension_{singular}_logs "
                        "WHERE dimension_id=:id ORDER BY dimension_version, field_name"
                    ),
                    {"id": UUID(original["id"])},
                )
            )
            .mappings()
            .all()
        )
    assert len(logs) == 4
    for log, operation, version, old, new, role, reason in (
        (logs[2], "DELETE", 2, False, True, "ADMIN", "삭제 사유"),
        (logs[3], "RESTORE", 3, True, False, "SUPER_ADMIN", None),
    ):
        assert (
            log["operation"],
            log["dimension_version"],
            log["field_name"],
            log["old_value"],
            log["new_value"],
            log["actor_kind"],
            log["actor_role"],
            log["actor_id"],
            log["reason"],
        ) == (operation, version, "DELETED", old, new, "HUMAN", role, str(ADMIN_ID), reason)
        expected = deleted if operation == "DELETE" else restored
        assert log["changed_at"] == datetime.fromisoformat(expected.json()["updated_at"])
    assert logs[2]["change_set_id"] != logs[3]["change_set_id"]


async def test_lifecycle_input_and_permission_failures_never_change_state(lifecycle_client) -> None:
    client, tokens, engine = lifecycle_client
    base = "/api/v1/dimensions/companies"
    created = await client.post(base, headers=_headers(tokens), json={"code": "A1", "value": "A"})
    path = f"{base}/{created.json()['id']}"
    for suffix in ("delete", "restore", "tombstone"):
        for role in (None, "USER"):
            headers = {} if role is None else _headers(tokens, role, '"1"')
            response = (
                await client.get(f"{path}/{suffix}", headers=headers)
                if suffix == "tombstone"
                else await client.post(f"{path}/{suffix}", headers=headers, json={})
            )
            assert response.status_code == (401 if role is None else 403)
    for action in ("delete", "restore"):
        for body in (
            "",
            "null",
            "[]",
            '{"extra":1}',
            '{"reason":1}',
            '{"reason":"a\\nb"}',
            '{"reason":"' + "a" * 501 + '"}',
        ):
            response = await client.post(
                f"{path}/{action}",
                content=body,
                headers={**_headers(tokens, etag='"1"'), "Content-Type": "application/json"},
            )
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "VALIDATION_ERROR"
        for etag, status in ((None, 428), ("*", 400), ('W/"1"', 400), ('"1", "2"', 400)):
            response = await client.post(
                f"{path}/{action}", json={}, headers=_headers(tokens, etag=etag)
            )
            assert response.status_code == status, response.text
    stale = await client.post(f"{path}/delete", json={}, headers=_headers(tokens, etag='"2"'))
    assert stale.status_code == 412
    for suffix in ("delete", "restore", "tombstone"):
        missing = f"{base}/{uuid4()}/{suffix}"
        response = (
            await client.get(missing, headers=_headers(tokens))
            if suffix == "tombstone"
            else await client.post(missing, json={}, headers=_headers(tokens, etag='"1"'))
        )
        assert response.status_code == 404
    assert (await client.get(path, headers=_headers(tokens))).json() == created.json()
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM dimension_company_logs")) == 2


@pytest.mark.parametrize(("singular", "plural", "value"), CASES)
async def test_active_references_block_deletion_but_deleted_mastercode_keeps_its_fk(
    lifecycle_client,
    singular: str,
    plural: str,
    value: Any,
) -> None:
    client, tokens, engine = lifecycle_client
    headers = _headers(tokens)
    base = f"/api/v1/dimensions/{plural}"
    created = await client.post(base, headers=headers, json={"code": "A1", "value": value})
    dimension_id = created.json()["id"]
    path = f"{base}/{dimension_id}"
    dimensions = {name: {"mode": "NOT_APPLICABLE"} for name, _, _ in CASES}
    dimensions[singular] = {"mode": "REFERENCE", "id": dimension_id}
    master = await client.post(
        "/api/v1/master-codes", headers=headers, json={"dimensions": dimensions}
    )
    assert master.status_code == 201, master.text
    refused = await client.post(f"{path}/delete", json={}, headers=_headers(tokens, etag='"1"'))
    assert refused.status_code == 409
    assert refused.json()["code"] == "DIMENSION_IN_USE"
    assert (await client.get(path, headers=headers)).json() == created.json()
    async with engine.begin() as conn:
        assert await conn.scalar(text(f"SELECT count(*) FROM dimension_{singular}_logs")) == 2
        # #31 owns the MasterCode lifecycle API; construct only its pre-existing tombstone state.
        await conn.execute(text("ALTER TABLE master_codes DISABLE TRIGGER USER"))
        await conn.execute(
            text("UPDATE master_codes SET deleted_at=updated_at WHERE id=:id"),
            {"id": UUID(master.json()["id"])},
        )
        await conn.execute(text("ALTER TABLE master_codes ENABLE TRIGGER USER"))
    deleted = await client.post(f"{path}/delete", json={}, headers=_headers(tokens, etag='"1"'))
    assert deleted.status_code == 200, deleted.text
    async with engine.connect() as conn:
        assert await conn.scalar(
            text(f"SELECT {singular}_id FROM master_codes WHERE id=:id"),
            {"id": UUID(master.json()["id"])},
        ) == UUID(dimension_id)
    invalid = await client.post(
        "/api/v1/master-codes", headers=headers, json={"dimensions": dimensions}
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_DIMENSION_REFERENCE"


@pytest.mark.parametrize(("singular", "plural", "value"), CASES)
@pytest.mark.parametrize("operation", ["delete", "restore"])
async def test_failed_lifecycle_log_rolls_back_state_and_log(
    lifecycle_client,
    singular: str,
    plural: str,
    value: Any,
    operation: str,
) -> None:
    client, tokens, engine = lifecycle_client
    base = f"/api/v1/dimensions/{plural}"
    original = await client.post(
        base, headers=_headers(tokens), json={"code": "A1", "value": value}
    )
    path = f"{base}/{original.json()['id']}"
    if operation == "restore":
        original = await client.post(
            f"{path}/delete", json={}, headers=_headers(tokens, etag='"1"')
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION reject_lifecycle_log() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private database diagnostic'; END $$"
            )
        )
        await conn.execute(
            text(
                f"CREATE TRIGGER reject_lifecycle_log BEFORE INSERT "
                f"ON dimension_{singular}_logs FOR EACH ROW EXECUTE FUNCTION reject_lifecycle_log()"
            )
        )
    try:
        failed = await client.post(
            f"{path}/{operation}", json={}, headers=_headers(tokens, etag=original.headers["etag"])
        )
        assert failed.status_code == 500
        assert "private database diagnostic" not in failed.text
        read = path if operation == "delete" else f"{path}/tombstone"
        after = await client.get(read, headers=_headers(tokens))
        assert after.json() == original.json()
        assert after.headers["etag"] == original.headers["etag"]
        async with engine.connect() as conn:
            assert await conn.scalar(text(f"SELECT count(*) FROM dimension_{singular}_logs")) == (
                2 if operation == "delete" else 3
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"DROP TRIGGER reject_lifecycle_log ON dimension_{singular}_logs")
            )
            await conn.execute(text("DROP FUNCTION reject_lifecycle_log()"))


@pytest.mark.parametrize("creation_first", [True, False])
async def test_dimension_delete_and_mastercode_creation_serialize_on_dimension_lock(
    lifecycle_client,
    monkeypatch: pytest.MonkeyPatch,
    creation_first: bool,
) -> None:
    import asyncio

    from tests.integration.test_master_code_persistence import _audit, _company_reference_plan

    from mdm.application.dimension_lifecycle import DimensionInUse
    from mdm.application.master_codes import InvalidDimensionReference
    from mdm.domain.audit import DimensionOperation
    from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
    from mdm.infrastructure.database import create_session_factory
    from mdm.infrastructure.repositories.dimension_lifecycle import (
        SqlAlchemyCompanyLifecycleRepository,
    )
    from mdm.infrastructure.repositories.master_codes import SqlAlchemyMasterCodeRepository

    client, tokens, engine = lifecycle_client
    created = await client.post(
        "/api/v1/dimensions/companies", headers=_headers(tokens), json={"code": "A1", "value": "A"}
    )
    dimension_id = UUID(created.json()["id"])
    sessions = create_session_factory(engine)
    lifecycle = SqlAlchemyCompanyLifecycleRepository(sessions)
    master = SqlAlchemyMasterCodeRepository(sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    started = asyncio.Event()
    original_reference = master._lock_reference
    original_context = SqlAlchemyMutationAuditContextWriter.set_context

    async def hold_reference(session, slot, target_id):
        result = await original_reference(session, slot, target_id)
        locked.set()
        await release.wait()
        return result

    async def hold_deletion_context(self, metadata, *, minimum_timestamp=None):
        if metadata.operations.dimension == DimensionOperation.DELETE:
            locked.set()
            await release.wait()
        return await original_context(self, metadata, minimum_timestamp=minimum_timestamp)

    if creation_first:
        monkeypatch.setattr(master, "_lock_reference", hold_reference)
    else:
        monkeypatch.setattr(
            SqlAlchemyMutationAuditContextWriter, "set_context", hold_deletion_context
        )

    async def delete():
        return await lifecycle.delete(dimension_id, 1, _lifecycle_audit(DimensionOperation.DELETE))

    async def create():
        return await master.create(_company_reference_plan(dimension_id), _audit(inline=False))

    async def second():
        started.set()
        return await (delete() if creation_first else create())

    first_task = asyncio.create_task(create() if creation_first else delete())
    second_task = None
    try:
        await asyncio.wait_for(locked.wait(), timeout=5)
        second_task = asyncio.create_task(second())
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_task), timeout=0.15)
        # Confirm actual PostgreSQL lock contention, beyond merely scheduling both tasks.
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
        results = await asyncio.gather(
            first_task, *([second_task] if second_task else []), return_exceptions=True
        )
    assert not isinstance(results[0], BaseException), results
    assert isinstance(results[1], DimensionInUse if creation_first else InvalidDimensionReference)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM master_codes")) == (
            1 if creation_first else 0
        )
        assert await conn.scalar(text("SELECT count(*) FROM dimension_company_logs")) == (
            2 if creation_first else 3
        )
        deleted = await conn.scalar(
            text("SELECT deleted_at FROM dimension_companies WHERE id=:id"), {"id": dimension_id}
        )
        assert (deleted is None) == creation_first


def _lifecycle_audit(operation):
    from mdm.application.audit import HumanMutationAuditFactory
    from mdm.application.auth import HumanPrincipal
    from mdm.infrastructure.uuid7 import Uuid7Generator

    return HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new).create(
        HumanPrincipal(ADMIN_ID, UserRole.ADMIN), dimension_operation=operation, reason="직접 검증"
    )


@pytest.mark.parametrize(("singular", "plural", "value"), CASES)
async def test_database_rejects_invalid_lifecycle_writes_and_log_tampering(
    lifecycle_client,
    singular: str,
    plural: str,
    value: Any,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from mdm.domain.audit import DimensionOperation
    from mdm.infrastructure.audit_context import SqlAlchemyMutationAuditContextWriter
    from mdm.infrastructure.database import create_session_factory

    client, tokens, engine = lifecycle_client
    created = await client.post(
        f"/api/v1/dimensions/{plural}",
        headers=_headers(tokens),
        json={"code": "A1", "value": value},
    )
    identifier = UUID(created.json()["id"])
    sessions = create_session_factory(engine)
    valid = "deleted_at=:now, updated_at=:now, version=version+1"
    raw_value = (
        "amount=2"
        if singular == "memory"
        else "value=2001"
        if singular == "year"
        else ("value=4" if singular == "network" else "value='OTHER'")
    )
    bad_mutations = [
        ("deleted_at=:now, updated_at=:now", DimensionOperation.DELETE),
        ("deleted_at=:now, version=version+1", DimensionOperation.DELETE),
        (valid + ", code='B1'", DimensionOperation.DELETE),
        (valid + ", " + raw_value, DimensionOperation.DELETE),
        (valid, DimensionOperation.UPDATE),
        ("deleted_at=NULL, updated_at=:now, version=version+1", DimensionOperation.RESTORE),
        (valid, None),
    ]
    for assignment, operation in bad_mutations:
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                if operation is not None:
                    now = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                        _lifecycle_audit(operation)
                    )
                else:
                    now = datetime.now(UTC)
                await session.execute(
                    text(f"UPDATE dimension_{plural} SET {assignment} WHERE id=:id"),
                    {"id": identifier, "now": now},
                )
    # Valid SQL must also satisfy active-reference protection, not only the repository check.
    dimensions = {name: {"mode": "NOT_APPLICABLE"} for name, _, _ in CASES}
    dimensions[singular] = {"mode": "REFERENCE", "id": str(identifier)}
    master = await client.post(
        "/api/v1/master-codes", headers=_headers(tokens), json={"dimensions": dimensions}
    )
    assert master.status_code == 201
    with pytest.raises(DBAPIError):
        async with sessions.begin() as session:
            now = await SqlAlchemyMutationAuditContextWriter(session).set_context(
                _lifecycle_audit(DimensionOperation.DELETE)
            )
            await session.execute(
                text(f"UPDATE dimension_{plural} SET {valid} WHERE id=:id"),
                {"id": identifier, "now": now},
            )
    for statement in (
        f"DELETE FROM dimension_{plural} WHERE id=:id",
        f"DELETE FROM dimension_{singular}_logs WHERE dimension_id=:id",
        f"UPDATE dimension_{singular}_logs SET reason='tampered' WHERE dimension_id=:id",
    ):
        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"id": identifier})
    assert (
        await client.get(f"/api/v1/dimensions/{plural}/{identifier}", headers=_headers(tokens))
    ).json() == created.json()
    async with engine.connect() as conn:
        assert await conn.scalar(text(f"SELECT count(*) FROM dimension_{singular}_logs")) == 2
