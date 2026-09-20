import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import text

from mdm.application.auth import EncodedAccessToken, HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.password_lifecycle import (
    InvalidPasswordResetToken,
    PasswordLifecycle,
    RequestPasswordReset,
)
from mdm.application.sessions import InvalidCredentials, InvalidSession, SessionUseCases
from mdm.application.user_management import UserManagement, UserManagementUnavailable
from mdm.application.users import NewUser
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, UserRole, UserStatus
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.email_delivery import InMemoryEmailSender
from mdm.infrastructure.repositories.password_lifecycle import SqlAlchemyPasswordRepository
from mdm.infrastructure.repositories.sessions import SqlAlchemySessionRepository
from mdm.infrastructure.repositories.user_management import SqlAlchemyUserManagementRepository
from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.integration
OLD = PlainPassword("valid old password phrase")
NEW = PlainPassword("valid new password phrase")


class TestHasher:
    async def hash(self, password):
        await asyncio.sleep(0)
        return password.reveal()

    async def verify(self, password, encoded_hash):
        await asyncio.sleep(0)
        return password.reveal() == encoded_hash


async def rows(factory, table):
    async with factory() as session:
        return list((await session.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings())


def token_from(sender):
    from mdm.domain.credentials import PasswordResetToken

    return PasswordResetToken(sender.deliveries[-1].body.split("#token=")[1].strip())


TABLES = (
    "users",
    "refresh_families",
    "refresh_tokens",
    "password_reset_challenges",
    "password_reset_tokens",
    "user_security_events",
)


@pytest.fixture
async def managed():
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users CASCADE"))
    factory = create_session_factory(engine)
    repository = SqlAlchemyPasswordRepository(factory)
    hasher, sink, signer = TestHasher(), Mock(), Mock()
    signer.issue.return_value = EncodedAccessToken("test-access")
    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    passwords = PasswordLifecycle(repository, hasher, sink, clock=lambda: datetime.now(UTC))
    request = RequestPasswordReset(repository, sender, sink, clock=lambda: datetime.now(UTC))
    sessions = SessionUseCases(
        SqlAlchemySessionRepository(factory),
        hasher,
        signer,
        sink,
        fake_password_hash="fake-password-hash",
        clock=lambda: datetime.now(UTC),
    )
    async with factory.begin() as session:
        user = await SqlAlchemyUserRepository(session).add(
            NewUser(
                EmailAddress("person@example.net"),
                DisplayName("대상"),
                OLD.reveal(),
                UserRole.USER,
                UserStatus.ACTIVE,
            )
        )
        admin = await SqlAlchemyUserRepository(session).add(
            NewUser(
                EmailAddress("admin@example.net"),
                DisplayName("관리자"),
                OLD.reveal(),
                UserRole.SUPER_ADMIN,
                UserStatus.ACTIVE,
            )
        )
    management_repo = SqlAlchemyUserManagementRepository(factory)
    try:
        yield SimpleNamespace(
            engine=engine,
            user=user,
            admin=admin,
            principal=HumanPrincipal(admin.id, admin.role),
            passwords=passwords,
            request=request,
            sender=sender,
            sink=sink,
            factory=factory,
            sessions=sessions,
            repository=management_repo,
            management=UserManagement(management_repo, AuthorizationPolicy()),
        )
    finally:
        await engine.dispose()


async def snapshot(ctx):
    return {t: await rows(ctx.factory, t) for t in TABLES}


async def target(ctx):
    return next(row for row in await rows(ctx.factory, "users") if row["id"] == ctx.user.id)


async def test_role_change_audit_noop_and_next_refresh_role(managed):
    c = managed
    grant = await c.sessions.login(c.user.email, OLD)
    await c.request.execute(c.user.email)
    before = await snapshot(c)
    await c.management.change_role(c.principal, c.user.id, UserRole.ADMIN)
    after = await snapshot(c)
    assert (await target(c))["role"] == "ADMIN"
    for table in TABLES[1:-1]:
        assert before[table] == after[table]
    audit = after["user_security_events"][0]
    assert (audit["previous_role"], audit["new_role"]) == ("USER", "ADMIN")
    assert audit["event_type"] == "USER_ROLE_CHANGED"
    assert audit["subject_user_id"] == c.user.id
    assert (audit["initiator_user_id"], audit["initiator_role"]) == (c.admin.id, "SUPER_ADMIN")
    assert audit["occurred_at"] == (await target(c))["updated_at"]
    await c.management.change_role(c.principal, c.user.id, UserRole.ADMIN)
    assert await snapshot(c) == after
    await c.sessions.refresh(grant.refresh_token)
    assert c.sessions._signer.issue.call_args.args[1] == UserRole.ADMIN


async def test_admin_promotion_then_retry_obeys_current_target_permissions(managed):
    c = managed
    admin = HumanPrincipal(c.admin.id, UserRole.ADMIN)
    await c.management.change_role(admin, c.user.id, UserRole.ADMIN)
    after = await snapshot(c)
    with pytest.raises(AuthorizationDenied):
        await c.management.change_role(admin, c.user.id, UserRole.ADMIN)
    assert await snapshot(c) == after


async def test_disable_and_reenable_never_restore_credentials_or_delayed_email(managed):
    c = managed
    grant = await c.sessions.login(c.user.email, OLD)
    await c.request.execute(c.user.email)
    token = token_from(c.sender)
    await c.management.change_status(c.principal, c.user.id, UserStatus.DISABLED)
    disabled = await snapshot(c)
    assert (await target(c))["status"] == "DISABLED"
    family = disabled["refresh_families"][0]
    challenge = disabled["password_reset_challenges"][0]
    audit = disabled["user_security_events"][0]
    assert family["status"] == challenge["status"] == "REVOKED"
    assert family["revoked_at"] == challenge["revoked_at"] == audit["occurred_at"]
    assert (audit["event_type"], audit["previous_status"], audit["new_status"]) == (
        "USER_DISABLED",
        "ACTIVE",
        "DISABLED",
    )
    assert audit["previous_role"] is None and audit["new_role"] is None
    await c.management.change_status(c.principal, c.user.id, UserStatus.DISABLED)
    assert await snapshot(c) == disabled
    with pytest.raises(InvalidCredentials):
        await c.sessions.login(c.user.email, OLD)
    with pytest.raises(InvalidSession):
        await c.sessions.refresh(grant.refresh_token)
    with pytest.raises(InvalidPasswordResetToken):
        await c.passwords.complete_reset(token, NEW)
    await c.request.execute(c.user.email)
    assert len(c.sender.deliveries) == 1
    await c.management.change_status(c.principal, c.user.id, UserStatus.ACTIVE)
    enabled = await snapshot(c)
    assert (await target(c))["status"] == "ACTIVE"
    for table in TABLES[1:-1]:
        assert enabled[table] == disabled[table]
    assert enabled["user_security_events"][-1]["event_type"] == "USER_ENABLED"
    assert len(c.sender.deliveries) == 1
    with pytest.raises(InvalidSession):
        await c.sessions.refresh(grant.refresh_token)
    with pytest.raises(InvalidPasswordResetToken):
        await c.passwords.complete_reset(token, NEW)
    await c.request.execute(c.user.email)
    await c.sessions.login(c.user.email, OLD)
    assert len(c.sender.deliveries) == 2


@pytest.mark.parametrize("action", ["role", "disable", "enable"])
async def test_audit_failure_rolls_back_every_changed_row(managed, action):
    c = managed
    await c.sessions.login(c.user.email, OLD)
    await c.request.execute(c.user.email)
    if action == "enable":
        await c.management.change_status(c.principal, c.user.id, UserStatus.DISABLED)
    before = await snapshot(c)
    async with c.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION reject_management_audit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test rejection'; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER reject_management_audit BEFORE INSERT ON user_security_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_management_audit()"
            )
        )
    try:
        with pytest.raises(UserManagementUnavailable):
            if action == "role":
                await c.management.change_role(c.principal, c.user.id, UserRole.ADMIN)
            else:
                await c.management.change_status(
                    c.principal,
                    c.user.id,
                    UserStatus.ACTIVE if action == "enable" else UserStatus.DISABLED,
                )
        assert await snapshot(c) == before
    finally:
        async with c.engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER reject_management_audit ON user_security_events"))
            await conn.execute(text("DROP FUNCTION reject_management_audit()"))


@pytest.mark.parametrize("action", ["role", "status"])
async def test_two_super_admins_mutually_change_to_zero_without_deadlock(managed, action):
    c = managed
    await c.management.change_role(c.principal, c.user.id, UserRole.SUPER_ADMIN)
    barrier = asyncio.Barrier(2)

    class RendezvousRepository(SqlAlchemyUserManagementRepository):
        @asynccontextmanager
        async def transaction(self, user_id):
            async with super().transaction(user_id) as tx:
                # Both target locks must exist before either audit checks its actor FK.
                await barrier.wait()
                yield tx

    use_case = UserManagement(RendezvousRepository(c.factory), AuthorizationPolicy())
    second = HumanPrincipal(c.user.id, UserRole.SUPER_ADMIN)
    if action == "role":
        calls = [
            use_case.change_role(c.principal, c.user.id, UserRole.USER),
            use_case.change_role(second, c.admin.id, UserRole.USER),
        ]
    else:
        calls = [
            use_case.change_status(c.principal, c.user.id, UserStatus.DISABLED),
            use_case.change_status(second, c.admin.id, UserStatus.DISABLED),
        ]
    await asyncio.wait_for(asyncio.gather(*calls), 10)
    users = await rows(c.factory, "users")
    assert not any(u["role"] == "SUPER_ADMIN" and u["status"] == "ACTIVE" for u in users)
    assert len(await rows(c.factory, "user_security_events")) == 3


@pytest.mark.parametrize(
    "first,second", [("role", "role"), ("status", "status"), ("role", "status")]
)
async def test_same_target_mutations_serialize_without_duplicate_audits(managed, first, second):
    c = managed
    barrier = asyncio.Barrier(2)

    async def run(action):
        await barrier.wait()
        if action == "role":
            await c.management.change_role(c.principal, c.user.id, UserRole.ADMIN)
        else:
            await c.management.change_status(c.principal, c.user.id, UserStatus.DISABLED)

    await asyncio.wait_for(asyncio.gather(run(first), run(second)), 10)
    audits = await rows(c.factory, "user_security_events")
    assert len(audits) == len({first, second})
    user = await target(c)
    if "role" in (first, second):
        assert user["role"] == "ADMIN"
    if "status" in (first, second):
        assert user["status"] == "DISABLED"


@pytest.mark.parametrize("other", ["login", "refresh", "logout", "reset", "change", "request"])
async def test_disable_races_real_credential_use_cases_without_partial_commit(managed, other):
    c = managed
    grant = await c.sessions.login(c.user.email, OLD)
    await c.request.execute(c.user.email)
    token = token_from(c.sender)
    operation = {
        "login": lambda: c.sessions.login(c.user.email, OLD),
        "refresh": lambda: c.sessions.refresh(grant.refresh_token),
        "logout": lambda: c.sessions.logout(grant.refresh_token),
        "reset": lambda: c.passwords.complete_reset(token, NEW),
        "change": lambda: c.passwords.change_password(
            HumanPrincipal(c.user.id, c.user.role), grant.refresh_token, OLD, NEW
        ),
        "request": lambda: c.request.execute(c.user.email),
    }[other]
    results = await asyncio.wait_for(
        asyncio.gather(
            c.management.change_status(c.principal, c.user.id, UserStatus.DISABLED),
            operation(),
            return_exceptions=True,
        ),
        10,
    )
    assert results[0] is None
    assert not isinstance(results[1], Exception) or isinstance(
        results[1], (InvalidCredentials, InvalidSession, InvalidPasswordResetToken)
    )
    assert (await target(c))["status"] == "DISABLED"
    assert all(r["status"] != "ACTIVE" for r in await rows(c.factory, "refresh_families"))
    assert all(r["status"] != "ACTIVE" for r in await rows(c.factory, "password_reset_challenges"))
    audits = await rows(c.factory, "user_security_events")
    assert sum(r["event_type"] == "USER_DISABLED" for r in audits) == 1
    mutation_succeeded = other in ("reset", "change") and not isinstance(results[1], Exception)
    assert len(audits) == (2 if mutation_succeeded else 1)
    assert (await target(c))["password_hash"] == (
        NEW.reveal() if mutation_succeeded else OLD.reveal()
    )


async def test_repository_failure_is_sanitized(managed, monkeypatch):
    from sqlalchemy.exc import OperationalError

    repo = managed.repository

    def fail_begin():
        raise OperationalError("secret SQL", {}, RuntimeError("private credentials"))

    monkeypatch.setattr(managed.factory, "begin", fail_begin)
    with pytest.raises(UserManagementUnavailable) as exc:
        async with repo.transaction(UUID(int=2)):
            pass
    assert "private" not in str(exc.value) and "secret" not in str(exc.value)


async def test_production_http_management_preserves_old_jwt_until_exact_skew_boundary(
    managed, monkeypatch, tmp_path
):
    from httpx import ASGITransport, AsyncClient

    from mdm.infrastructure.jwt import build_access_jwt_codec
    from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
    from mdm.main import create_app

    c = managed
    private_key, jwks = tmp_path / "private.pem", tmp_path / "public.json"
    generate_local_jwt_keys(kid="management-test", private_key_path=private_key, jwks_path=jwks)
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "management-test")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private_key))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(jwks))
    codec = build_access_jwt_codec(Settings())
    c.sessions._signer = codec
    original = await c.sessions.login(c.user.email, OLD)
    issued_at = int(original.issued_at.timestamp())
    admin_token = codec.issue(c.admin.id, UserRole.SUPER_ADMIN, issued_at=issued_at)
    admin_headers = {"Authorization": f"Bearer {admin_token.reveal()}"}
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            promoted = await client.put(
                f"/api/v1/admin/users/{c.user.id}/role",
                headers=admin_headers,
                json={"role": "SUPER_ADMIN"},
            )
            assert promoted.status_code == 204
            old_user = await client.get(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {original.access_token.reveal()}"},
            )
            assert old_user.status_code == 403
            refreshed = await c.sessions.refresh(original.refresh_token)
            new_headers = {"Authorization": f"Bearer {refreshed.access_token.reveal()}"}
            assert (await client.get("/api/v1/admin/users", headers=new_headers)).status_code == 200
            disabled = await client.put(
                f"/api/v1/admin/users/{c.admin.id}/status",
                headers=new_headers,
                json={"status": "DISABLED"},
            )
            assert disabled.status_code == 204
            # Status does not invalidate the already-issued bearer principal.
            demoted = await client.put(
                f"/api/v1/admin/users/{c.admin.id}/role", headers=new_headers, json={"role": "USER"}
            )
            assert demoted.status_code == 204

            class Clock:
                now_value = issued_at + 929

                @staticmethod
                def now(tz):
                    return datetime.fromtimestamp(Clock.now_value, tz=tz)

            monkeypatch.setattr("mdm.main.datetime", Clock)
            still_valid = await client.get("/api/v1/admin/users", headers=admin_headers)
            assert still_valid.status_code == 200
            Clock.now_value = issued_at + 930
            expired = await client.get("/api/v1/admin/users", headers=admin_headers)
            assert expired.status_code == 401
            assert expired.json()["code"] == "INVALID_ACCESS_TOKEN"
