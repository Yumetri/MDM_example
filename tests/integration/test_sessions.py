from collections.abc import AsyncGenerator
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.auth import EncodedAccessToken
from mdm.application.sessions import SessionUseCases
from mdm.application.users import NewUser
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, UserRole, UserStatus
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository
from mdm.infrastructure.settings import Settings


class TestHasher:
    async def verify(self, password: PlainPassword, encoded_hash: str) -> bool:
        return password.reveal() == encoded_hash

    async def hash(self, password: PlainPassword) -> str:
        return password.reveal()


@pytest.fixture
async def session_engine() -> AsyncGenerator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users, user_security_events CASCADE"))
    yield engine
    await engine.dispose()


async def make_use_cases(engine: AsyncEngine):
    from mdm.infrastructure.repositories.sessions import SqlAlchemySessionRepository

    factory = create_session_factory(engine)
    async with factory.begin() as session:
        user = await SqlAlchemyUserRepository(session).add(
            NewUser(
                email=EmailAddress("person@example.net"),
                name=DisplayName("사용자"),
                password_hash="valid password here",
                role=UserRole.USER,
                status=UserStatus.ACTIVE,
            )
        )
    signer = Mock()
    signer.issue.return_value = EncodedAccessToken("signed-access")
    use_cases = SessionUseCases(
        SqlAlchemySessionRepository(factory),
        TestHasher(),
        signer,
        Mock(),
        fake_password_hash="fake-password-hash",
        clock=lambda: user.created_at,
    )
    return use_cases, user, factory


@pytest.mark.integration
async def test_login_replaces_family_and_persists_only_refresh_digest(session_engine: AsyncEngine):
    from mdm.infrastructure.models import RefreshFamilyRecord, RefreshTokenRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    first = await use_cases.login(user.email, PlainPassword("valid password here"))
    second = await use_cases.login(user.email, PlainPassword("valid password here"))
    async with factory() as session:
        families = list((await session.scalars(select(RefreshFamilyRecord))).all())
        tokens = list((await session.scalars(select(RefreshTokenRecord))).all())
    assert sorted(family.status for family in families) == ["ACTIVE", "REVOKED"]
    assert second.expires_at - second.issued_at == timedelta(days=7)
    assert {token.digest for token in tokens} == {
        first.refresh_token.digest(),
        second.refresh_token.digest(),
    }
    assert first.refresh_token != second.refresh_token
    assert all(not hasattr(token, "raw_token") for token in tokens)


@pytest.mark.integration
async def test_rotation_conflict_reuse_and_used_token_logout(
    session_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    from mdm.application.sessions import InvalidSession, RefreshConflict
    from mdm.infrastructure.models import RefreshFamilyRecord
    from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction

    use_cases, user, factory = await make_use_cases(session_engine)
    first = await use_cases.login(user.email, PlainPassword("valid password here"))
    rotated = await use_cases.refresh(first.refresh_token)
    assert rotated.expires_at == first.expires_at
    assert rotated.refresh_token != first.refresh_token
    await use_cases.logout(first.refresh_token)
    async with factory() as session:
        assert await session.scalar(select(RefreshFamilyRecord.status)) == "ACTIVE"
    with pytest.raises(RefreshConflict):
        await use_cases.refresh(first.refresh_token)

    async def after_grace(self):
        return rotated.issued_at + timedelta(seconds=11)

    monkeypatch.setattr(SqlAlchemySessionTransaction, "now", after_grace)
    with pytest.raises(InvalidSession):
        await use_cases.refresh(first.refresh_token)
    async with factory() as session:
        assert await session.scalar(select(RefreshFamilyRecord.status)) == "REVOKED"
    with pytest.raises(InvalidSession):
        await use_cases.refresh(rotated.refresh_token)


@pytest.mark.integration
async def test_current_token_logout_revokes_once_and_is_idempotent(session_engine: AsyncEngine):
    from mdm.application.sessions import InvalidSession
    from mdm.infrastructure.models import RefreshFamilyRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))
    await use_cases.logout(grant.refresh_token)
    async with factory() as session:
        revoked_at = await session.scalar(select(RefreshFamilyRecord.revoked_at))
    await use_cases.logout(grant.refresh_token)
    async with factory() as session:
        assert await session.scalar(select(RefreshFamilyRecord.revoked_at)) == revoked_at
    with pytest.raises(InvalidSession):
        await use_cases.refresh(grant.refresh_token)


@pytest.mark.integration
async def test_real_http_login_profile_refresh_logout(
    session_engine: AsyncEngine, monkeypatch, tmp_path, capsys
):
    from httpx import ASGITransport, AsyncClient

    from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
    from mdm.infrastructure.passwords import build_password_hasher
    from mdm.main import create_app

    private_key, public_keys = tmp_path / "private.pem", tmp_path / "public.json"
    generate_local_jwt_keys(kid="session-test", private_key_path=private_key, jwks_path=public_keys)
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "session-test")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private_key))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(public_keys))
    monkeypatch.setenv("MDM_AUTH_SESSIONS_ENABLED", "true")
    monkeypatch.setenv("MDM_AUTH_ALLOWED_ORIGINS", '["https://app.example.net"]')
    monkeypatch.setenv("MDM_AUTH_IP_HMAC_SECRET", "A" * 43)
    factory = create_session_factory(session_engine)
    password_hash = await build_password_hasher(Settings()).hash(
        PlainPassword("valid password here")
    )
    async with factory.begin() as session:
        await SqlAlchemyUserRepository(session).add(
            NewUser(
                email=EmailAddress("person@example.net"),
                name=DisplayName("사용자"),
                password_hash=password_hash,
                role=UserRole.USER,
                status=UserStatus.ACTIVE,
            )
        )
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            blocked = await client.post("/api/v1/auth/login", content=b"not-json")
            assert blocked.status_code == 403
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://app.example.net"},
                json={"email": "PERSON@example.net", "password": "valid password here"},
            )
            assert login.status_code == 200, login.text
            assert login.json()["expires_in"] == 900
            assert set(login.json()) == {"access_token", "token_type", "expires_in"}
            assert login.headers["cache-control"] == "no-store"
            old_refresh = client.cookies["mdm_refresh"]
            old_csrf = client.cookies["mdm_csrf"]
            me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            )
            assert me.status_code == 200
            assert me.json()["email"] == "person@example.net"
            assert me.json()["effective_role"] == "USER"
            denied = await client.post(
                "/api/v1/auth/session/refresh", headers={"Origin": "https://app.example.net"}
            )
            assert denied.status_code == 403
            assert "set-cookie" not in denied.headers
            refreshed = await client.post(
                "/api/v1/auth/session/refresh",
                headers={
                    "Origin": "https://app.example.net",
                    "X-CSRF-Token": old_csrf,
                },
            )
            assert refreshed.status_code == 200, refreshed.text
            assert client.cookies["mdm_refresh"] != old_refresh
            assert client.cookies["mdm_csrf"] != old_csrf
            conflict = await client.post(
                "/api/v1/auth/session/refresh",
                headers={
                    "Origin": "https://app.example.net",
                    "X-CSRF-Token": old_csrf,
                    "Cookie": f"mdm_refresh={old_refresh}; mdm_csrf={old_csrf}",
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "refresh_conflict"
            assert conflict.headers["retry-after"] == "1"
            assert "set-cookie" not in conflict.headers
            logout = await client.delete(
                "/api/v1/auth/session",
                headers={
                    "Origin": "https://app.example.net",
                    "X-CSRF-Token": client.cookies["mdm_csrf"],
                },
            )
            assert logout.status_code == 204
            assert not logout.content
            assert "mdm_refresh" not in client.cookies
            assert "mdm_csrf" not in client.cookies

            failures = []
            for email, password in (
                ("missing@example.net", "valid password here"),
                ("person@example.net", "wrong password here"),
            ):
                failed = await client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": "https://app.example.net"},
                    json={"email": email, "password": password},
                )
                assert failed.status_code == 401
                failures.append(failed.json())
            async with factory.begin() as session:
                await session.execute(text("UPDATE users SET status='DISABLED'"))
            disabled = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "https://app.example.net"},
                json={"email": "person@example.net", "password": "valid password here"},
            )
            assert disabled.status_code == 401
            assert failures[0] == failures[1] == disabled.json()
            invalid = await client.post(
                "/api/v1/auth/session/refresh",
                headers={
                    "Origin": "https://app.example.net",
                    "X-CSRF-Token": old_csrf,
                    "Cookie": f"mdm_refresh=malformed; mdm_csrf={old_csrf}",
                },
            )
            assert invalid.status_code == 401
            assert invalid.json()["code"] == "INVALID_SESSION"
            assert len(invalid.headers.get_list("set-cookie")) == 2
    output = capsys.readouterr()
    assert output.err.count('"event":"LOGIN_FAILED"') == 3
    assert output.err.count('"event":"REFRESH_CONFLICT"') == 1
    for secret in (old_refresh, old_csrf, login.json()["access_token"], password_hash):
        assert secret not in output.err + output.out


@pytest.mark.integration
@pytest.mark.parametrize(
    "actions",
    [("login", "login"), ("refresh", "refresh"), ("login", "refresh"), ("logout", "refresh")],
)
async def test_credential_mutations_contend_on_user_lock_without_deadlock(
    session_engine: AsyncEngine,
    actions: tuple[str, str],
):
    import asyncio

    from mdm.application.sessions import InvalidSession, RefreshConflict
    from mdm.infrastructure.models import RefreshFamilyRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))

    async def perform(action: str):
        if action == "login":
            return await use_cases.login(user.email, PlainPassword("valid password here"))
        if action == "refresh":
            return await use_cases.refresh(grant.refresh_token)
        return await use_cases.logout(grant.refresh_token)

    tasks = []
    try:
        async with factory.begin() as blocker:
            await blocker.execute(
                text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": user.id}
            )
            tasks = [asyncio.create_task(perform(action)) for action in actions]
            # Observe real database waiters before releasing the barrier.
            for _ in range(100):
                async with factory() as observer:
                    waiters = await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND query LIKE '%users%'"
                        )
                    )
                if waiters >= 2:
                    break
                await asyncio.sleep(0.01)
            assert waiters >= 2
            assert all(not task.done() for task in tasks)
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        assert all(
            not isinstance(result, Exception)
            or isinstance(result, (InvalidSession, RefreshConflict))
            for result in results
        )
        if actions == ("login", "login"):
            assert all(not isinstance(result, Exception) for result in results)
        if actions == ("refresh", "refresh"):
            assert sum(isinstance(result, RefreshConflict) for result in results) == 1
        async with factory() as session:
            active = list(
                (
                    await session.scalars(
                        select(RefreshFamilyRecord).where(RefreshFamilyRecord.status == "ACTIVE")
                    )
                ).all()
            )
            assert len(active) <= 1
            if actions != ("logout", "refresh"):
                assert len(active) == 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.integration
@pytest.mark.parametrize("delta", [9.999999, 10, 10.000001])
async def test_refresh_grace_uses_exact_database_time_boundary(session_engine, monkeypatch, delta):
    from mdm.application.sessions import InvalidSession, RefreshConflict
    from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction

    use_cases, user, _ = await make_use_cases(session_engine)
    first = await use_cases.login(user.email, PlainPassword("valid password here"))
    rotated = await use_cases.refresh(first.refresh_token)

    async def now(self):
        return rotated.issued_at + timedelta(seconds=delta)

    monkeypatch.setattr(SqlAlchemySessionTransaction, "now", now)
    with pytest.raises(RefreshConflict if delta <= 10 else InvalidSession):
        await use_cases.refresh(first.refresh_token)


@pytest.mark.integration
async def test_refresh_rejects_exact_absolute_expiry(session_engine, monkeypatch):
    from mdm.application.sessions import InvalidSession
    from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction

    use_cases, user, _ = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))

    async def now(self):
        return grant.expires_at

    monkeypatch.setattr(SqlAlchemySessionTransaction, "now", now)
    with pytest.raises(InvalidSession):
        await use_cases.refresh(grant.refresh_token)


@pytest.mark.integration
async def test_digest_collision_exhaustion_rolls_back_login_replacement(
    session_engine, monkeypatch
):
    from mdm.application.tokens import OpaqueTokenCollision
    from mdm.infrastructure.models import RefreshFamilyRecord, RefreshTokenRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))
    generate = Mock(return_value=grant.refresh_token)
    monkeypatch.setattr(use_cases, "_refresh_tokens", generate)
    with pytest.raises(OpaqueTokenCollision):
        await use_cases.login(user.email, PlainPassword("valid password here"))
    assert generate.call_count == 4
    async with factory() as session:
        families = list((await session.scalars(select(RefreshFamilyRecord))).all())
        tokens = list((await session.scalars(select(RefreshTokenRecord))).all())
    assert len(families) == len(tokens) == 1
    assert families[0].status == "ACTIVE"
    assert tokens[0].digest == grant.refresh_token.digest()


@pytest.mark.integration
async def test_signing_failure_rolls_back_rotation_and_token_consumption(
    session_engine, monkeypatch
):
    from mdm.infrastructure.models import RefreshFamilyRecord, RefreshTokenRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))
    monkeypatch.setattr(
        use_cases._signer, "issue", Mock(side_effect=RuntimeError("signing failed"))
    )
    with pytest.raises(RuntimeError, match="signing failed"):
        await use_cases.refresh(grant.refresh_token)
    async with factory() as session:
        tokens = list((await session.scalars(select(RefreshTokenRecord))).all())
        assert await session.scalar(select(RefreshFamilyRecord.status)) == "ACTIVE"
    assert len(tokens) == 1
    assert tokens[0].used_at is None
    assert tokens[0].replaced_by_id is None


@pytest.mark.integration
async def test_digest_collision_retry_keeps_rotation_atomic(session_engine, monkeypatch):
    from mdm.domain.credentials import RefreshToken, generate_opaque_token
    from mdm.infrastructure.models import RefreshTokenRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    grant = await use_cases.login(user.email, PlainPassword("valid password here"))
    fresh = generate_opaque_token(RefreshToken)
    generate = Mock(side_effect=[grant.refresh_token, grant.refresh_token, fresh])
    monkeypatch.setattr(use_cases, "_refresh_tokens", generate)
    rotated = await use_cases.refresh(grant.refresh_token)
    assert rotated.refresh_token == fresh
    assert generate.call_count == 3
    async with factory() as session:
        rows = list((await session.scalars(select(RefreshTokenRecord))).all())
    assert len(rows) == 2
    old = next(row for row in rows if row.digest == grant.refresh_token.digest())
    new = next(row for row in rows if row.digest == fresh.digest())
    assert old.replaced_by_id == new.id
    assert old.used_at == new.issued_at


@pytest.mark.integration
async def test_database_enforces_single_active_family_and_digest_shape(session_engine):
    from sqlalchemy.exc import IntegrityError

    from mdm.infrastructure.models import RefreshFamilyRecord

    use_cases, user, factory = await make_use_cases(session_engine)
    await use_cases.login(user.email, PlainPassword("valid password here"))
    async with factory() as session:
        family_id = await session.scalar(select(RefreshFamilyRecord.id))
    statements = (
        (
            "INSERT INTO refresh_families (user_id, created_at, updated_at, expires_at) "
            "VALUES (:id, clock_timestamp(), clock_timestamp(), "
            "clock_timestamp()+interval '7 days')",
            {"id": user.id},
        ),
        (
            "INSERT INTO refresh_tokens (family_id, digest, issued_at) "
            "VALUES (:id, decode('00','hex'), clock_timestamp())",
            {"id": family_id},
        ),
        ("UPDATE refresh_families SET status='REVOKED' WHERE id=:id", {"id": family_id}),
        ("UPDATE refresh_tokens SET replaced_by_id=id WHERE family_id=:id", {"id": family_id}),
    )
    for statement, values in statements:
        with pytest.raises(IntegrityError):
            async with factory.begin() as session:
                await session.execute(text(statement), values)


@pytest.mark.integration
async def test_password_verification_releases_real_database_connection(session_engine, monkeypatch):
    use_cases, user, _ = await make_use_cases(session_engine)

    async def verify(password, encoded_hash):
        assert session_engine.sync_engine.pool.checkedout() == 0
        return True

    monkeypatch.setattr(use_cases._password_hasher, "verify", verify)
    await use_cases.login(user.email, PlainPassword("valid password here"))
