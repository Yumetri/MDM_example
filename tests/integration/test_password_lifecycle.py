import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.auth import EncodedAccessToken, HumanPrincipal
from mdm.application.email_delivery import EmailDeliveryStatus
from mdm.application.password_lifecycle import (
    InvalidCurrentPassword,
    InvalidPasswordResetToken,
    PasswordLifecycle,
    RequestPasswordReset,
)
from mdm.application.sessions import InvalidCredentials, InvalidSession, SessionUseCases
from mdm.application.users import NewUser
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, UserRole, UserStatus
from mdm.domain.credentials import PasswordResetToken
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.email_delivery import InMemoryEmailSender
from mdm.infrastructure.models import UserRecord
from mdm.infrastructure.repositories.sessions import SqlAlchemySessionRepository
from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.integration
OLD = PlainPassword("valid old password phrase")
NEW = PlainPassword("valid new password phrase")


@pytest.fixture
async def password_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users CASCADE"))
    try:
        yield engine
    finally:
        await engine.dispose()


class TestHasher:
    async def hash(self, password):
        await asyncio.sleep(0)
        return password.reveal()

    async def verify(self, password, encoded_hash):
        await asyncio.sleep(0)
        return password.reveal() == encoded_hash


async def services(engine, *, outcomes=()):
    from mdm.infrastructure.repositories.password_lifecycle import SqlAlchemyPasswordRepository

    factory = create_session_factory(engine)
    async with factory.begin() as session:
        user = await SqlAlchemyUserRepository(session).add(
            NewUser(
                EmailAddress("person@example.net"),
                DisplayName("사용자"),
                OLD.reveal(),
                UserRole.USER,
                UserStatus.ACTIVE,
            )
        )
    repository = SqlAlchemyPasswordRepository(factory)
    hasher = TestHasher()
    sink = Mock()
    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net", outcomes=outcomes)
    lifecycle = PasswordLifecycle(repository, hasher, sink, clock=lambda: datetime.now(UTC))
    request = RequestPasswordReset(repository, sender, sink, clock=lambda: datetime.now(UTC))
    signer = Mock()
    signer.issue.return_value = EncodedAccessToken("test-access")
    sessions = SessionUseCases(
        SqlAlchemySessionRepository(factory),
        hasher,
        signer,
        Mock(),
        fake_password_hash="fake-password-hash",
        clock=lambda: datetime.now(UTC),
    )
    return user, lifecycle, request, sender, sink, factory, sessions, repository


def token_from(sender, index=-1):
    return PasswordResetToken(sender.deliveries[index].body.split("#token=")[1].strip())


async def rows(factory, table):
    async with factory() as session:
        return list((await session.execute(text(f"SELECT * FROM {table} ORDER BY id"))).mappings())


async def test_reset_request_persists_digest_and_reuses_absolute_deadline(password_engine):
    user, _, request, sender, _, factory, _, _ = await services(password_engine)
    await request.execute(user.email)
    first = (await rows(factory, "password_reset_challenges"))[0]
    await request.execute(user.email)
    challenges = await rows(factory, "password_reset_challenges")
    tokens = await rows(factory, "password_reset_tokens")
    assert len(challenges) == 1 and len(tokens) == 2
    assert challenges[0]["expires_at"] == first["expires_at"]
    assert first["expires_at"] - first["created_at"] == timedelta(minutes=30)
    assert {r["digest"] for r in tokens} == {token_from(sender, i).digest() for i in (0, 1)}
    assert "token" not in tokens[0] and "password_hash" not in challenges[0]


async def test_request_ignores_unknown_and_disabled_users(password_engine):
    user, _, request, sender, sink, factory, _, _ = await services(password_engine)
    await request.execute(EmailAddress("missing@example.net"))
    async with factory.begin() as session:
        await session.execute(update(UserRecord).values(status="DISABLED"))
    await request.execute(user.email)
    assert not sender.deliveries
    assert not await rows(factory, "password_reset_challenges")
    assert not await rows(factory, "password_reset_tokens")
    sink.emit.assert_not_called()


async def test_reset_completion_revokes_family_invalidates_all_sibling_tokens_and_audits(
    password_engine,
):
    user, lifecycle, request, sender, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    await request.execute(user.email)
    await lifecycle.complete_reset(token_from(sender, 0), NEW)
    assert (await rows(factory, "users"))[0]["password_hash"] == NEW.reveal()
    assert (await rows(factory, "refresh_families"))[0]["status"] == "REVOKED"
    challenge = (await rows(factory, "password_reset_challenges"))[0]
    assert challenge["status"] == "COMPLETED" and challenge["completed_at"] is not None
    assert sum(t["used_at"] is not None for t in await rows(factory, "password_reset_tokens")) == 1
    audit = (await rows(factory, "user_security_events"))[0]
    assert audit["event_type"] == "PASSWORD_RESET" and audit["initiator_type"] == "RESET_TOKEN"
    assert audit["initiator_user_id"] is None and audit["initiator_role"] is None
    for index in (0, 1):
        with pytest.raises(InvalidPasswordResetToken):
            await lifecycle.complete_reset(token_from(sender, index), OLD)
    with pytest.raises(InvalidSession):
        await sessions.refresh(grant.refresh_token)
    with pytest.raises(InvalidCredentials):
        await sessions.login(user.email, OLD)
    await sessions.login(user.email, NEW)


async def test_change_password_rotates_family_revokes_reset_and_preserves_old_access_role(
    password_engine,
):
    user, lifecycle, request, sender, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    principal = HumanPrincipal(user.id, UserRole.ADMIN)
    changed = await lifecycle.change_password(principal, grant.refresh_token, OLD, NEW)
    assert changed.expires_at - changed.issued_at == timedelta(days=7)
    assert sorted(r["status"] for r in await rows(factory, "refresh_families")) == [
        "ACTIVE",
        "REVOKED",
    ]
    assert (await rows(factory, "password_reset_challenges"))[0]["status"] == "REVOKED"
    audit = (await rows(factory, "user_security_events"))[0]
    assert audit["event_type"] == "PASSWORD_CHANGED"
    assert audit["initiator_user_id"] == user.id and audit["initiator_role"] == "ADMIN"
    with pytest.raises(InvalidPasswordResetToken):
        await lifecycle.complete_reset(token_from(sender), OLD)
    with pytest.raises(InvalidSession):
        await sessions.refresh(grant.refresh_token)
    await sessions.refresh(changed.refresh_token)


@pytest.mark.parametrize("outcome", [EmailDeliveryStatus.REJECTED, EmailDeliveryStatus.TIMED_OUT])
async def test_reset_email_after_commit_and_connection_release_preserves_failed_delivery(
    password_engine, outcome
):
    from mdm.application.email_delivery import EmailDeliveryResult

    user, _, _, _, sink, factory, _, repository = await services(password_engine)

    class Sender:
        async def send(self, message):
            assert password_engine.sync_engine.pool.checkedout() == 0
            stored = await rows(factory, "password_reset_tokens")
            assert stored[0]["digest"] == message.token.digest()
            return EmailDeliveryResult(outcome)

    request = RequestPasswordReset(repository, Sender(), sink, clock=lambda: datetime.now(UTC))
    await request.execute(user.email)
    assert (await rows(factory, "password_reset_challenges"))[0]["status"] == "ACTIVE"
    sink.emit.assert_called_once()
    assert sink.emit.call_args.args[0].name == "EMAIL_DELIVERY_FAILED"


async def test_reset_hash_and_password_verification_never_hold_a_database_connection(
    password_engine,
):
    user, _, request, sender, sink, _, sessions, repository = await services(password_engine)

    class CheckingHasher(TestHasher):
        async def hash(self, password):
            assert password_engine.sync_engine.pool.checkedout() == 0
            return await super().hash(password)

        async def verify(self, password, encoded_hash):
            assert password_engine.sync_engine.pool.checkedout() == 0
            return await super().verify(password, encoded_hash)

    lifecycle = PasswordLifecycle(
        repository, CheckingHasher(), sink, clock=lambda: datetime.now(UTC)
    )
    grant = await sessions.login(user.email, OLD)
    await lifecycle.change_password(
        HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW
    )
    await request.execute(user.email)
    await lifecycle.complete_reset(token_from(sender), OLD)


async def test_concurrent_reset_requests_serialize_one_challenge(password_engine):
    user, _, request, sender, _, factory, _, _ = await services(password_engine)
    await asyncio.wait_for(asyncio.gather(*(request.execute(user.email) for _ in range(8))), 10)
    assert len(await rows(factory, "password_reset_challenges")) == 1
    assert len(await rows(factory, "password_reset_tokens")) == len(sender.deliveries) == 8


async def test_concurrent_completions_commit_one_password_and_audit(password_engine):
    user, lifecycle, request, sender, _, factory, _, _ = await services(password_engine)
    await request.execute(user.email)
    await request.execute(user.email)
    results = await asyncio.wait_for(
        asyncio.gather(
            lifecycle.complete_reset(token_from(sender, 0), NEW),
            lifecycle.complete_reset(token_from(sender, 1), OLD),
            return_exceptions=True,
        ),
        10,
    )
    assert sum(r is None for r in results) == 1
    assert sum(isinstance(r, InvalidPasswordResetToken) for r in results) == 1
    assert len(await rows(factory, "user_security_events")) == 1


async def test_password_change_failure_does_not_mutate_any_credentials(password_engine):
    user, lifecycle, request, _, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    tables = (
        "users",
        "refresh_families",
        "refresh_tokens",
        "password_reset_challenges",
        "password_reset_tokens",
        "user_security_events",
    )
    before = {t: await rows(factory, t) for t in tables}
    with pytest.raises(InvalidCurrentPassword):
        await lifecycle.change_password(
            HumanPrincipal(user.id, user.role), grant.refresh_token, NEW, NEW
        )
    assert {t: await rows(factory, t) for t in tables} == before


async def test_mismatch_does_not_mutate_other_users_session(password_engine):
    from uuid import uuid4

    user, lifecycle, request, _, sink, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    tables = (
        "users",
        "refresh_families",
        "refresh_tokens",
        "password_reset_challenges",
        "password_reset_tokens",
        "user_security_events",
    )
    before = {t: await rows(factory, t) for t in tables}
    with pytest.raises(InvalidSession):
        await lifecycle.change_password(
            HumanPrincipal(uuid4(), user.role), grant.refresh_token, OLD, NEW
        )
    assert {t: await rows(factory, t) for t in tables} == before
    sink.emit.assert_called_once()
    await sessions.refresh(grant.refresh_token)


async def test_expired_reset_is_replaced_without_deleting_retained_tokens(password_engine):
    user, lifecycle, request, sender, _, factory, _, _ = await services(password_engine)
    await request.execute(user.email)
    old_token = token_from(sender)
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE password_reset_challenges SET "
                "created_at=clock_timestamp()-interval '31 minutes', "
                "expires_at=clock_timestamp()-interval '1 minute'"
            )
        )
    with pytest.raises(InvalidPasswordResetToken):
        await lifecycle.complete_reset(old_token, NEW)
    await request.execute(user.email)
    assert sorted(r["status"] for r in await rows(factory, "password_reset_challenges")) == [
        "ACTIVE",
        "EXPIRED",
    ]
    assert len(await rows(factory, "password_reset_tokens")) == 2
    await lifecycle.complete_reset(token_from(sender), NEW)


@pytest.mark.parametrize("flow", ["reset", "change"])
async def test_audit_failure_rolls_back_password_sessions_and_challenge(password_engine, flow):
    from mdm.application.password_lifecycle import PasswordLifecycleUnavailable

    user, lifecycle, request, sender, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    tables = (
        "users",
        "refresh_families",
        "refresh_tokens",
        "password_reset_challenges",
        "password_reset_tokens",
        "user_security_events",
    )
    before = {t: await rows(factory, t) for t in tables}
    async with password_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE FUNCTION reject_test_password_audit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test audit failure'; END $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER test_password_audit_failure "
                "BEFORE INSERT ON user_security_events FOR EACH ROW "
                "EXECUTE FUNCTION reject_test_password_audit()"
            )
        )
    try:
        with pytest.raises(PasswordLifecycleUnavailable):
            if flow == "reset":
                await lifecycle.complete_reset(token_from(sender), NEW)
            else:
                await lifecycle.change_password(
                    HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW
                )
        assert {t: await rows(factory, t) for t in tables} == before
    finally:
        async with password_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER test_password_audit_failure ON user_security_events")
            )
            await connection.execute(text("DROP FUNCTION reject_test_password_audit()"))


@pytest.mark.parametrize("flow", ["reset", "change"])
@pytest.mark.parametrize("other", ["refresh", "logout", "login"])
async def test_password_mutation_and_session_operations_serialize_without_deadlocks(
    password_engine, flow, other
):
    user, lifecycle, request, sender, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    mutation = (
        lifecycle.complete_reset(token_from(sender), NEW)
        if flow == "reset"
        else lifecycle.change_password(
            HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW
        )
    )
    operation = {
        "refresh": lambda: sessions.refresh(grant.refresh_token),
        "logout": lambda: sessions.logout(grant.refresh_token),
        "login": lambda: sessions.login(user.email, OLD),
    }[other]()
    results = await asyncio.wait_for(
        asyncio.gather(mutation, operation, return_exceptions=True), 10
    )
    assert all(
        not isinstance(r, Exception) or isinstance(r, (InvalidSession, InvalidCredentials))
        for r in results
    )
    audits = await rows(factory, "user_security_events")
    users = await rows(factory, "users")
    families = await rows(factory, "refresh_families")
    if not isinstance(results[0], Exception):
        assert users[0]["password_hash"] == NEW.reveal()
        assert len(audits) == 1
        assert sum(f["status"] == "ACTIVE" for f in families) == (0 if flow == "reset" else 1)
    else:
        assert users[0]["password_hash"] == OLD.reveal() and not audits


async def test_reset_and_password_change_race_commits_exactly_one_mutation(password_engine):
    user, lifecycle, request, sender, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    results = await asyncio.wait_for(
        asyncio.gather(
            lifecycle.complete_reset(token_from(sender), NEW),
            lifecycle.change_password(
                HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW
            ),
            return_exceptions=True,
        ),
        10,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert (
        sum(
            isinstance(r, (InvalidSession, InvalidPasswordResetToken, InvalidCurrentPassword))
            for r in results
        )
        == 1
    )
    assert len(await rows(factory, "user_security_events")) == 1
    assert (await rows(factory, "users"))[0]["password_hash"] == NEW.reveal()


async def test_used_refresh_rejection_keeps_current_family_even_after_grace(password_engine):
    user, lifecycle, request, _, _, factory, sessions, _ = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    current = await sessions.refresh(grant.refresh_token)
    await request.execute(user.email)
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE refresh_tokens SET issued_at=issued_at-interval '1 minute', "
                "used_at=used_at-interval '20 seconds' WHERE used_at IS NOT NULL"
            )
        )
    tables = (
        "users",
        "refresh_families",
        "refresh_tokens",
        "password_reset_challenges",
        "password_reset_tokens",
        "user_security_events",
    )
    before = {t: await rows(factory, t) for t in tables}
    with pytest.raises(InvalidSession):
        await lifecycle.change_password(
            HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW
        )
    assert {t: await rows(factory, t) for t in tables} == before
    await sessions.refresh(current.refresh_token)


@pytest.mark.parametrize("kind", ["reset", "refresh"])
async def test_digest_collision_exhaustion_rolls_back_whole_transaction(password_engine, kind):
    from mdm.application.tokens import OpaqueTokenCollision

    user, _, request, sender, sink, factory, sessions, repository = await services(password_engine)
    grant = await sessions.login(user.email, OLD)
    await request.execute(user.email)
    tables = (
        "users",
        "refresh_families",
        "refresh_tokens",
        "password_reset_challenges",
        "password_reset_tokens",
        "user_security_events",
    )
    before = {t: await rows(factory, t) for t in tables}
    if kind == "reset":
        tokens = Mock(return_value=token_from(sender))
        operation = RequestPasswordReset(
            repository, sender, sink, clock=lambda: datetime.now(UTC), tokens=tokens
        ).execute(user.email)
    else:
        tokens = Mock(return_value=grant.refresh_token)
        operation = PasswordLifecycle(
            repository, TestHasher(), sink, clock=lambda: datetime.now(UTC), refresh_tokens=tokens
        ).change_password(HumanPrincipal(user.id, user.role), grant.refresh_token, OLD, NEW)
    with pytest.raises(OpaqueTokenCollision):
        await operation
    assert tokens.call_count == 4
    assert {t: await rows(factory, t) for t in tables} == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE password_reset_challenges SET status='UNKNOWN'",
        "UPDATE password_reset_challenges SET status='COMPLETED'",
        "UPDATE password_reset_challenges SET status='REVOKED'",
        "UPDATE password_reset_challenges SET expires_at=created_at",
        "UPDATE password_reset_challenges SET status='COMPLETED', completed_at=expires_at",
        "UPDATE password_reset_challenges SET status='REVOKED', "
        "revoked_at=created_at-interval '1 second'",
        "UPDATE password_reset_tokens SET digest=decode('abcd','hex')",
        "UPDATE password_reset_tokens SET used_at=issued_at-interval '1 second'",
        "UPDATE password_reset_challenges SET user_id=uuidv7()",
        "UPDATE password_reset_tokens SET challenge_id=uuidv7()",
        "DELETE FROM password_reset_challenges",
        "INSERT INTO password_reset_challenges (user_id, created_at, expires_at) "
        "SELECT user_id, created_at, expires_at FROM password_reset_challenges",
        "INSERT INTO password_reset_tokens (challenge_id, digest, issued_at) "
        "SELECT challenge_id, digest, issued_at FROM password_reset_tokens",
    ],
)
async def test_reset_schema_rejects_invalid_state_times_duplicates_and_foreign_keys(
    password_engine, statement
):
    from sqlalchemy.exc import IntegrityError

    user, _, request, _, _, factory, _, _ = await services(password_engine)
    await request.execute(user.email)
    async with factory.begin() as session:
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text(statement))


@pytest.mark.parametrize("resets_enabled", [False, True])
async def test_real_app_password_change_and_reset_composition(
    password_engine, monkeypatch, tmp_path, capsys, resets_enabled
):
    from httpx import ASGITransport, AsyncClient

    from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
    from mdm.infrastructure.passwords import build_password_hasher
    from mdm.main import create_app

    private, public = tmp_path / "private.pem", tmp_path / "public.json"
    generate_local_jwt_keys(kid="password-test", private_key_path=private, jwks_path=public)
    for name, value in {
        "MDM_AUTH_JWT_ACTIVE_KID": "password-test",
        "MDM_AUTH_JWT_PRIVATE_KEY_PATH": str(private),
        "MDM_AUTH_JWT_JWKS_PATH": str(public),
        "MDM_AUTH_SESSIONS_ENABLED": "true",
        "MDM_AUTH_REGISTRATIONS_ENABLED": "false",
        "MDM_AUTH_PASSWORD_RESETS_ENABLED": str(resets_enabled).lower(),
        "MDM_AUTH_ALLOWED_ORIGINS": '["https://app.example.net"]',
        "MDM_AUTH_IP_HMAC_SECRET": "A" * 43,
    }.items():
        monkeypatch.setenv(name, value)
    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    if resets_enabled:
        for name, value in {
            "MDM_EMAIL_PUBLIC_APP_BASE_URL": "https://app.example.net",
            "MDM_EMAIL_SMTP_HOST": "smtp.example.net",
            "MDM_EMAIL_SMTP_PORT": "587",
            "MDM_EMAIL_SMTP_USERNAME": "test-user",
            "MDM_EMAIL_SMTP_PASSWORD": "test-secret",
            "MDM_EMAIL_SENDER_ADDRESS": "noreply@example.net",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr("mdm.auth_sessions.build_smtp_email_sender", lambda settings: sender)
    factory = create_session_factory(password_engine)
    password_hash = await build_password_hasher(Settings()).hash(OLD)
    async with factory.begin() as session:
        await SqlAlchemyUserRepository(session).add(
            NewUser(
                EmailAddress("person@example.net"),
                DisplayName("사용자"),
                password_hash,
                UserRole.USER,
                UserStatus.ACTIVE,
            )
        )
    app = create_app()
    origin = "https://app.example.net"
    raw_reset: str | None = None
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url=origin) as client:
            assert (
                await client.post("/api/v1/auth/registrations", content=b"invalid")
            ).status_code == 503
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": origin},
                json={"email": "person@example.net", "password": OLD.reveal()},
            )
            assert login.status_code == 200, login.text
            access = login.json()["access_token"]
            old_refresh = client.cookies["mdm_refresh"]
            old_csrf = client.cookies["mdm_csrf"]
            change = await client.put(
                "/api/v1/auth/me/password",
                headers={
                    "Origin": origin,
                    "Authorization": f"Bearer {access}",
                    "X-CSRF-Token": old_csrf,
                },
                json={"current_password": OLD.reveal(), "new_password": NEW.reveal()},
            )
            assert change.status_code == 204, change.text
            assert change.content == b""
            assert client.cookies["mdm_refresh"] != old_refresh
            assert client.cookies["mdm_csrf"] != old_csrf
            profile = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"}
            )
            assert profile.status_code == 200
            refresh = await client.post(
                "/api/v1/auth/session/refresh",
                headers={"Origin": origin, "X-CSRF-Token": client.cookies["mdm_csrf"]},
            )
            assert refresh.status_code == 200
            reset = await client.post(
                "/api/v1/auth/password-resets", json={"email": "person@example.net"}
            )
            assert reset.status_code == (202 if resets_enabled else 503), reset.text
            if resets_enabled:
                raw_reset = token_from(sender).reveal()
                complete = await client.post(
                    "/api/v1/auth/password-resets/complete",
                    json={"token": raw_reset, "new_password": OLD.reveal()},
                )
                assert complete.status_code == 204 and not complete.content
                assert "mdm_refresh" not in client.cookies and "mdm_csrf" not in client.cookies
                denied = await client.post(
                    "/api/v1/auth/password-resets/complete",
                    json={"token": raw_reset, "new_password": NEW.reveal()},
                )
                assert denied.status_code == 400
                login = await client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": origin},
                    json={"email": "person@example.net", "password": OLD.reveal()},
                )
                assert login.status_code == 200
    output = capsys.readouterr()
    for secret in (OLD.reveal(), NEW.reveal(), password_hash, access, old_refresh, old_csrf):
        assert secret not in output.out + output.err
    if resets_enabled:
        assert raw_reset is not None
        assert raw_reset not in output.out + output.err
