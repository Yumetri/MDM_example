from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, insert, select, text, update

from mdm.api.admin_users import admin_user_error_handler, build_admin_user_router
from mdm.api.errors import authorization_denied_handler, validation_error_handler
from mdm.application.admin_users import (
    AdminUserQueries,
    UserNotFound,
    UserQueryUnavailable,
    UserQueryValidationError,
)
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.auth import UserRole, UserStatus
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.models import UserRecord, UserSecurityEventRecord
from mdm.infrastructure.repositories.admin_users import SqlAlchemyAdminUserRepository
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.integration
PATH = "/api/v1/admin/users"
FIELDS = {"id", "email", "name", "role", "status", "created_at", "updated_at"}


@pytest.fixture
async def user_db():
    engine = create_engine(Settings().reveal_database_url())
    created = datetime(2026, 9, 17, tzinfo=UTC)
    users = [
        {
            "id": UUID(int=i),
            "normalized_email": f"reader{i}@example.com",
            "name": f"사용자 {i}",
            "password_hash": "private-credential",
            "role": role.value,
            "status": status.value,
            "created_at": created + timedelta(seconds=i // 4),
            "updated_at": created + timedelta(seconds=i // 4),
        }
        for i, (role, status) in enumerate(
            [(r, s) for _ in range(2) for r in UserRole for s in UserStatus],
            start=1,
        )
    ]
    users[0]["normalized_email"] = "strasse@xn--bcher-kva.de"
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE refresh_tokens, refresh_families, user_security_events, users")
        )
        await connection.execute(insert(UserRecord), users)
        await connection.execute(
            insert(UserSecurityEventRecord).values(
                event_type="INITIAL_SUPER_ADMIN_BOOTSTRAPPED",
                subject_user_id=UUID(int=5),
                initiator_type="BOOTSTRAP_CLI",
                new_role="SUPER_ADMIN",
                new_status="ACTIVE",
            )
        )
    repository = SqlAlchemyAdminUserRepository(create_session_factory(engine))
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield engine, repository, users, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        await engine.dispose()


def application(repository, role):
    app = FastAPI()
    policy = AuthorizationPolicy()
    app.include_router(
        build_admin_user_router(
            use_cases=AdminUserQueries(repository, policy),
            authorization=policy,
            principal_dependency=lambda: HumanPrincipal(UUID(int=5), role),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(AuthorizationDenied, authorization_denied_handler)
    for error in (UserNotFound, UserQueryUnavailable, UserQueryValidationError):
        app.add_exception_handler(error, admin_user_error_handler)
    return app


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
@pytest.mark.parametrize("limit", [1, 3, 5, 100])
async def test_visibility_precedes_pagination_and_pages_have_no_gaps_or_duplicates(
    user_db, role, limit
):
    engine, repository, users, statements = user_db
    app = application(repository, role)
    expected = sorted(
        (row for row in users if role == UserRole.SUPER_ADMIN or row["role"] == "USER"),
        key=lambda row: (row["created_at"], row["id"]),
        reverse=True,
    )
    items = []
    query = {"limit": str(limit)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(len(users) + 1):
            response = await client.get(PATH, params=query)
            assert response.status_code == 200, response.text
            page = response.json()
            assert set(page) == {"items", "next_cursor"}
            assert len(page["items"]) <= limit
            items.extend(page["items"])
            if page["next_cursor"] is None:
                break
            query["cursor"] = page["next_cursor"]
        else:
            pytest.fail("pagination did not terminate")
    assert [item["id"] for item in items] == [str(row["id"]) for row in expected]
    assert all(set(item) == FIELDS for item in items)
    assert all("password_hash" not in sql and "JOIN" not in sql for sql in statements)
    assert all(
        "LIMIT" in sql and "ORDER BY users.created_at DESC, users.id DESC" in sql
        for sql in statements
    )
    if role == UserRole.ADMIN:
        assert all("WHERE users.role =" in sql for sql in statements)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(UserSecurityEventRecord))
        ) == 1


@pytest.mark.parametrize("caller", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
@pytest.mark.parametrize("target_role", list(UserRole))
@pytest.mark.parametrize("status", list(UserStatus))
async def test_combined_role_status_filters_preserve_visibility(
    user_db, caller, target_role, status
):
    _, repository, users, statements = user_db
    app = application(repository, caller)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PATH, params={"role": target_role.value, "status": status.value}
        )
    assert response.status_code == 200
    expected = {
        str(row["id"])
        for row in users
        if row["role"] == target_role
        and row["status"] == status
        and (caller == UserRole.SUPER_ADMIN or row["role"] == "USER")
    }
    assert {item["id"] for item in response.json()["items"]} == expected
    assert len(statements) == 1
    if caller == UserRole.ADMIN:
        assert statements[0].count("users.role =") == 2


async def test_normalized_email_exact_search_and_empty_result(user_db):
    _, repository, _, _ = user_db
    app = application(repository, UserRole.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        match = await client.get(PATH, params={"email": "  Straße@BÜCHER.de ", "status": "ACTIVE"})
        assert match.status_code == 200
        assert [item["id"] for item in match.json()["items"]] == [str(UUID(int=1))]
        missing = await client.get(PATH, params={"email": "strass@xn--bcher-kva.de"})
        assert missing.json() == {"items": [], "next_cursor": None}
        hidden = await client.get(PATH, params={"email": "reader3@example.com"})
        assert hidden.json() == {"items": [], "next_cursor": None}


async def test_hidden_and_nonexistent_details_have_the_same_error(user_db):
    _, repository, _, statements = user_db
    app = application(repository, UserRole.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        hidden = await client.get(f"{PATH}/{UUID(int=3)}")
        missing = await client.get(f"{PATH}/{UUID(int=999)}")
        visible = await client.get(f"{PATH}/{UUID(int=2)}")
    assert hidden.status_code == missing.status_code == 404
    hidden_body, missing_body = hidden.json(), missing.json()
    hidden_body.pop("instance")
    missing_body.pop("instance")
    assert hidden_body == missing_body
    assert visible.status_code == 200
    assert visible.json()["status"] == "DISABLED"
    assert set(visible.json()) == FIELDS
    assert all("WHERE users.role =" in sql and "users.id =" in sql for sql in statements)
    assert all("password_hash" not in sql for sql in statements)


async def test_super_admin_reads_self_and_current_db_role_status(user_db):
    engine, repository, _, _ = user_db
    app = application(repository, UserRole.SUPER_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PATH}/{UUID(int=5)}")
        assert response.status_code == 200
        assert response.json()["role"] == "SUPER_ADMIN"
        async with engine.begin() as connection:
            await connection.execute(
                update(UserRecord)
                .where(UserRecord.id == UUID(int=5))
                .values(
                    role="ADMIN",
                    status="DISABLED",
                )
            )
        changed = await client.get(f"{PATH}/{UUID(int=5)}")
        assert changed.status_code == 200
        assert changed.json()["role"] == "ADMIN"
        assert changed.json()["status"] == "DISABLED"


async def test_admin_loses_visibility_immediately_when_target_role_changes(user_db):
    engine, repository, _, _ = user_db
    app = application(repository, UserRole.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"{PATH}/{UUID(int=1)}")).status_code == 200
        async with engine.begin() as connection:
            await connection.execute(
                update(UserRecord).where(UserRecord.id == UUID(int=1)).values(role="ADMIN")
            )
        assert (await client.get(f"{PATH}/{UUID(int=1)}")).status_code == 404


async def test_production_composition_uses_signed_jwt_role_and_real_database(
    user_db, monkeypatch, tmp_path
):
    from mdm.infrastructure.jwt import build_access_jwt_codec
    from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
    from mdm.main import create_app

    _, _, users, _ = user_db
    private_key = tmp_path / "private.pem"
    public_keys = tmp_path / "public.json"
    generate_local_jwt_keys(
        kid="admin-query-test", private_key_path=private_key, jwks_path=public_keys
    )
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "admin-query-test")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private_key))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(public_keys))
    monkeypatch.setenv("MDM_AUTH_SESSIONS_ENABLED", "false")
    codec = build_access_jwt_codec(Settings())
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        for role in UserRole:
            token = codec.issue(
                user_id=UUID(int=5), role=role, issued_at=int(datetime.now(UTC).timestamp())
            )
            response = await client.get(PATH, headers={"Authorization": f"Bearer {token.reveal()}"})
            if role == UserRole.USER:
                assert response.status_code == 403
                assert response.json()["code"] == "AUTHORIZATION_DENIED"
            else:
                assert response.status_code == 200, response.text
                expected = {
                    str(row["id"])
                    for row in users
                    if role == UserRole.SUPER_ADMIN or row["role"] == "USER"
                }
                assert {item["id"] for item in response.json()["items"]} == expected
                detail = await client.get(
                    f"{PATH}/{UUID(int=1)}", headers={"Authorization": f"Bearer {token.reveal()}"}
                )
                assert detail.status_code == 200
                assert set(detail.json()) == FIELDS


async def test_connection_refusal_returns_documented_503_for_both_routes():
    import socket

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        engine = create_engine(f"postgresql+asyncpg://mdm:mdm-local@127.0.0.1:{port}/mdm_test")
        try:
            app = application(
                SqlAlchemyAdminUserRepository(create_session_factory(engine)), UserRole.ADMIN
            )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for path in (PATH, f"{PATH}/{UUID(int=1)}"):
                    response = await client.get(path)
                    assert response.status_code == 503, response.text
                    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
                    assert "127.0.0.1" not in response.text
                    assert "mdm-local" not in response.text
        finally:
            await engine.dispose()
