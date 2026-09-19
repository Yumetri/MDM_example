import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.application.auth import EncodedAccessToken
from mdm.application.email_delivery import EmailDeliveryStatus
from mdm.application.registrations import (
    CompleteRegistration,
    InvalidRegistrationToken,
    RequestRegistration,
)
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword
from mdm.domain.credentials import RegistrationToken
from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.email_delivery import InMemoryEmailSender
from mdm.infrastructure.models import (
    RefreshFamilyRecord,
    RegistrationChallengeRecord,
    RegistrationTokenRecord,
    UserRecord,
)
from mdm.infrastructure.settings import Settings

pytestmark = pytest.mark.integration
EMAIL = EmailAddress("person@example.net")
PASSWORD = PlainPassword("valid registration password")
NAME = DisplayName("가입 사용자")


class CheckingHasher:
    def __init__(self, engine):
        self.engine = engine

    async def hash(self, password):
        assert self.engine.sync_engine.pool.checkedout() == 0
        await asyncio.sleep(0)
        return "test-password-hash"

    async def verify(self, password, encoded_hash):
        return encoded_hash == "test-password-hash"


@pytest.fixture
async def registration_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings().reveal_database_url())
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE registration_challenges, users CASCADE"))
    try:
        yield engine
    finally:
        await engine.dispose()


def services(engine, *, outcomes=()):
    from mdm.infrastructure.repositories.registrations import SqlAlchemyRegistrationRepository

    factory = create_session_factory(engine)
    repository = SqlAlchemyRegistrationRepository(factory)

    class CheckingSender(InMemoryEmailSender):
        async def send(self, message):
            assert engine.sync_engine.pool.checkedout() == 0
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(RegistrationTokenRecord.id).where(
                            RegistrationTokenRecord.digest == message.token.digest()
                        )
                    )
                    is not None
                )
            return await super().send(message)

    sender = CheckingSender(public_app_base_url="https://app.example.net", outcomes=outcomes)
    sink = Mock()
    signer = Mock()
    signer.issue.return_value = EncodedAccessToken("test-signed-token")
    request = RequestRegistration(
        repository, sender, sink, allowed_domains=("example.net",), clock=lambda: datetime.now(UTC)
    )
    complete = CompleteRegistration(
        repository, CheckingHasher(engine), signer, allowed_domains=("example.net",)
    )
    return request, complete, sender, sink, factory, signer


def emailed_token(sender, index=-1):
    return RegistrationToken(sender.deliveries[index].body.split("#token=")[1].strip())


async def test_registration_request_persists_only_digest_and_sends_after_commit(
    registration_engine,
):
    request, _, sender, sink, factory, _ = services(registration_engine)
    await request.execute(EMAIL)
    token = emailed_token(sender)
    async with factory() as session:
        challenge = (await session.scalars(select(RegistrationChallengeRecord))).one()
        stored = (await session.scalars(select(RegistrationTokenRecord))).one()
        assert await session.scalar(select(UserRecord.id)) is None
        assert stored.digest == token.digest()
        assert challenge.expires_at - challenge.created_at == timedelta(days=1)
        assert challenge.status == "ACTIVE"
        assert not hasattr(challenge, "password_hash")
    sink.emit.assert_not_called()


async def test_registration_resends_share_one_challenge_without_extending_deadline(
    registration_engine,
):
    request, _, sender, _, factory, _ = services(registration_engine)
    await request.execute(EMAIL)
    async with factory() as session:
        deadline = await session.scalar(select(RegistrationChallengeRecord.expires_at))
    await request.execute(EMAIL)
    async with factory() as session:
        challenges = list((await session.scalars(select(RegistrationChallengeRecord))).all())
        tokens = list((await session.scalars(select(RegistrationTokenRecord))).all())
    assert len(challenges) == 1
    assert challenges[0].expires_at == deadline
    assert len(tokens) == len(sender.deliveries) == 2


async def test_registration_completion_creates_one_user_and_session_and_invalidates_siblings(
    registration_engine,
):
    request, complete, sender, _, factory, _ = services(registration_engine)
    await request.execute(EMAIL)
    await request.execute(EMAIL)
    grant = await complete.execute(emailed_token(sender, 0), NAME, PASSWORD)
    async with factory() as session:
        user = (await session.scalars(select(UserRecord))).one()
        family = (await session.scalars(select(RefreshFamilyRecord))).one()
        challenge = (await session.scalars(select(RegistrationChallengeRecord))).one()
        assert user.role == "USER" and user.status == "ACTIVE"
        assert family.user_id == user.id
        assert family.expires_at == grant.expires_at
        assert challenge.status == "COMPLETED"
    for index in (0, 1):
        with pytest.raises(InvalidRegistrationToken):
            await complete.execute(emailed_token(sender, index), NAME, PASSWORD)


@pytest.mark.parametrize("outcome", [EmailDeliveryStatus.REJECTED, EmailDeliveryStatus.TIMED_OUT])
async def test_registration_email_failure_preserves_committed_challenge(
    registration_engine, outcome
):
    request, _, sender, sink, factory, _ = services(registration_engine, outcomes=(outcome,))
    await request.execute(EMAIL)
    async with factory() as session:
        assert await session.scalar(select(RegistrationChallengeRecord.status)) == "ACTIVE"
        assert (
            await session.scalar(select(RegistrationTokenRecord.digest))
            == emailed_token(sender).digest()
        )
    sink.emit.assert_called_once()


async def test_registration_signing_failure_rolls_back_user_session_and_challenge(
    registration_engine,
):
    from mdm.application.registrations import RegistrationUnavailable

    request, complete, sender, _, factory, signer = services(registration_engine)
    await request.execute(EMAIL)
    signer.issue.side_effect = ValueError("internal signing failure")
    with pytest.raises(RegistrationUnavailable):
        await complete.execute(emailed_token(sender), NAME, PASSWORD)
    async with factory() as session:
        assert await session.scalar(select(UserRecord.id)) is None
        assert await session.scalar(select(RefreshFamilyRecord.id)) is None
        assert await session.scalar(select(RegistrationChallengeRecord.status)) == "ACTIVE"
        assert await session.scalar(select(RegistrationTokenRecord.used_at)) is None


async def test_simultaneous_registration_requests_share_one_active_challenge(registration_engine):
    from mdm.infrastructure.repositories.registrations import SqlAlchemyRegistrationRepository

    factory = create_session_factory(registration_engine)
    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    request = RequestRegistration(
        SqlAlchemyRegistrationRepository(factory),
        sender,
        Mock(),
        allowed_domains=("example.net",),
        clock=lambda: datetime.now(UTC),
    )
    await asyncio.wait_for(asyncio.gather(*(request.execute(EMAIL) for _ in range(8))), timeout=10)
    async with factory() as session:
        challenges = list((await session.scalars(select(RegistrationChallengeRecord))).all())
        tokens = list((await session.scalars(select(RegistrationTokenRecord))).all())
    assert len(challenges) == 1
    assert len(tokens) == len(sender.deliveries) == 8
    assert {token.challenge_id for token in tokens} == {challenges[0].id}


async def test_simultaneous_registration_completions_commit_exactly_one_user(registration_engine):
    from mdm.infrastructure.repositories.registrations import SqlAlchemyRegistrationRepository

    request, _, sender, _, factory, signer = services(registration_engine)
    await request.execute(EMAIL)
    await request.execute(EMAIL)
    both_hashing = asyncio.Event()
    arrivals = 0

    class ConcurrentHasher:
        async def hash(self, password):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_hashing.set()
            await both_hashing.wait()
            return "test-password-hash"

        async def verify(self, password, encoded_hash):
            raise AssertionError("registration does not verify an existing password")

    complete = CompleteRegistration(
        SqlAlchemyRegistrationRepository(factory),
        ConcurrentHasher(),
        signer,
        allowed_domains=("example.net",),
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            complete.execute(emailed_token(sender, 0), NAME, PASSWORD),
            complete.execute(emailed_token(sender, 1), NAME, PASSWORD),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(isinstance(result, InvalidRegistrationToken) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    async with factory() as session:
        assert len((await session.scalars(select(UserRecord))).all()) == 1
        assert len((await session.scalars(select(RefreshFamilyRecord))).all()) == 1
        used = (
            await session.scalars(
                select(RegistrationTokenRecord).where(RegistrationTokenRecord.used_at.is_not(None))
            )
        ).all()
        assert len(used) == 1


async def test_expired_registration_request_creates_new_challenge_and_rejects_old_token(
    registration_engine,
):
    request, complete, sender, _, factory, _ = services(registration_engine)
    await request.execute(EMAIL)
    first = emailed_token(sender)
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE registration_challenges "
                "SET created_at=clock_timestamp()-interval '2 days', "
                "expires_at=clock_timestamp()-interval '1 day'"
            )
        )
    await request.execute(EMAIL)
    async with factory() as session:
        assert sorted(
            (await session.scalars(select(RegistrationChallengeRecord.status))).all()
        ) == ["ACTIVE", "EXPIRED"]
    with pytest.raises(InvalidRegistrationToken):
        await complete.execute(first, NAME, PASSWORD)
    await complete.execute(emailed_token(sender), NAME, PASSWORD)


@pytest.mark.parametrize("exhausted", [False, True])
async def test_real_registration_digest_collision_savepoint_and_full_rollback(
    registration_engine, exhausted
):
    from mdm.application.tokens import OpaqueTokenCollision
    from mdm.domain.credentials import generate_opaque_token
    from mdm.infrastructure.repositories.registrations import SqlAlchemyRegistrationRepository

    request, _, sender, sink, factory, _ = services(registration_engine)
    await request.execute(EMAIL)
    original = emailed_token(sender)
    unique = generate_opaque_token(RegistrationToken)
    tokens = iter([original] * (4 if exhausted else 1) + [unique])
    collision_request = RequestRegistration(
        SqlAlchemyRegistrationRepository(factory),
        sender,
        sink,
        allowed_domains=("example.net",),
        clock=lambda: datetime.now(UTC),
        tokens=lambda: next(tokens),
    )
    if exhausted:
        with pytest.raises(OpaqueTokenCollision):
            await collision_request.execute(EmailAddress("another@example.net"))
    else:
        await collision_request.execute(EmailAddress("another@example.net"))
    async with factory() as session:
        challenges = (await session.scalars(select(RegistrationChallengeRecord))).all()
        persisted = (await session.scalars(select(RegistrationTokenRecord.digest))).all()
    assert len(challenges) == len(sender.deliveries) == (1 if exhausted else 2)
    assert set(persisted) == (
        {original.digest()} if exhausted else {original.digest(), unique.digest()}
    )


def configure_http_registration(monkeypatch, tmp_path, sender):
    from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys

    private_key, public_keys = (
        tmp_path / "registration-key.pem",
        tmp_path / "registration-keys.json",
    )
    generate_local_jwt_keys(
        kid="registration-test", private_key_path=private_key, jwks_path=public_keys
    )
    settings = {
        "AUTH_JWT_ACTIVE_KID": "registration-test",
        "AUTH_JWT_PRIVATE_KEY_PATH": str(private_key),
        "AUTH_JWT_JWKS_PATH": str(public_keys),
        "AUTH_SESSIONS_ENABLED": "true",
        "AUTH_REGISTRATIONS_ENABLED": "true",
        "AUTH_ALLOWED_ORIGINS": '["https://app.example.net"]',
        "AUTH_IP_HMAC_SECRET": "A" * 43,
        "AUTH_REGISTRATION_ALLOWED_DOMAINS": '["example.net"]',
        "EMAIL_PUBLIC_APP_BASE_URL": "https://app.example.net",
        "EMAIL_SMTP_HOST": "smtp.example.net",
        "EMAIL_SMTP_PORT": "587",
        "EMAIL_SMTP_USERNAME": "test-user",
        "EMAIL_SMTP_PASSWORD": "test-smtp-password",
        "EMAIL_SENDER_ADDRESS": "noreply@example.net",
    }
    for name, value in settings.items():
        monkeypatch.setenv("MDM_" + name, value)
    monkeypatch.setattr("mdm.auth_sessions.build_smtp_email_sender", lambda _: sender)


async def test_real_http_registration_auto_login_profile_refresh_and_regular_login(
    registration_engine, monkeypatch, tmp_path, capsys
):
    from httpx import ASGITransport, AsyncClient

    from mdm.main import create_app

    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    configure_http_registration(monkeypatch, tmp_path, sender)
    app = create_app()
    origin = {"Origin": "https://app.example.net"}
    path = "/api/v1/auth/registrations"
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            requested = await client.post(path, json={"email": EMAIL.value})
            assert requested.status_code == 202
            raw_token = emailed_token(sender).reveal()
            body = {"token": raw_token, "name": NAME.value, "password": PASSWORD.reveal()}
            blocked = await client.post(path + "/complete", content=b"not-json")
            assert blocked.status_code == 403
            invalid = await client.post(
                path + "/complete", json={**body, "password": "short"}, headers=origin
            )
            assert invalid.status_code == 422
            assert "set-cookie" not in invalid.headers
            complete = await client.post(path + "/complete", json=body, headers=origin)
            assert complete.status_code == 201, complete.text
            assert complete.headers["cache-control"] == "no-store"
            assert len(complete.headers.get_list("set-cookie")) == 2
            me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": "Bearer " + complete.json()["access_token"]},
            )
            assert me.status_code == 200
            assert me.json()["effective_role"] == "USER"
            assert me.json()["email"] == EMAIL.value
            reused = await client.post(path + "/complete", json=body, headers=origin)
            assert reused.status_code == 400
            assert reused.json()["code"] == "INVALID_REGISTRATION_TOKEN"
            assert "set-cookie" not in reused.headers
            repeated = await client.post(path, json={"email": EMAIL.value})
            assert repeated.status_code == 202 and repeated.json() == requested.json()
            assert len(sender.deliveries) == 1
            refresh = await client.post(
                "/api/v1/auth/session/refresh",
                headers={**origin, "X-CSRF-Token": client.cookies["mdm_csrf"]},
            )
            assert refresh.status_code == 200
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": EMAIL.value, "password": PASSWORD.reveal()},
                headers=origin,
            )
            assert login.status_code == 200
    output = capsys.readouterr()
    for secret in (
        raw_token,
        PASSWORD.reveal(),
        EMAIL.value,
        "test-smtp-password",
        complete.json()["access_token"],
    ):
        assert secret not in output.out + output.err


@pytest.mark.parametrize("outcome", [EmailDeliveryStatus.REJECTED, EmailDeliveryStatus.TIMED_OUT])
async def test_real_http_email_failure_keeps_same_202_as_existing_account(
    registration_engine, monkeypatch, tmp_path, outcome, capsys
):
    from httpx import ASGITransport, AsyncClient

    from mdm.main import create_app

    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net", outcomes=(outcome,))
    configure_http_registration(monkeypatch, tmp_path, sender)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            response = await client.post("/api/v1/auth/registrations", json={"email": EMAIL.value})
            assert response.status_code == 202
            assert response.json() == {
                "message": "가입 가능한 이메일이면 인증 안내를 보냈습니다. 메일함을 확인해 주세요."
            }
    async with create_session_factory(registration_engine)() as session:
        assert await session.scalar(select(RegistrationChallengeRecord.status)) == "ACTIVE"
        assert await session.scalar(select(RegistrationTokenRecord.id)) is not None
    output = capsys.readouterr()
    assert (output.out + output.err).count('"event":"EMAIL_DELIVERY_FAILED"') == 1
    assert emailed_token(sender).reveal() not in output.out + output.err
    assert EMAIL.value not in output.out + output.err


async def test_http_registration_and_login_share_the_composition_root_quota(
    registration_engine, monkeypatch, tmp_path
):
    from ipaddress import ip_address

    from httpx import ASGITransport, AsyncClient

    from mdm.auth_protection import build_auth_protection
    from mdm.main import create_app

    class VerifiedIp:
        def resolve(self, *, peer_host, headers):
            return ip_address("192.0.2.1")

    def with_verified_ip(settings, *, event_sink):
        return build_auth_protection(settings, event_sink=event_sink, client_ips=VerifiedIp())

    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    configure_http_registration(monkeypatch, tmp_path, sender)
    monkeypatch.setattr("mdm.auth_sessions.build_auth_protection", with_verified_ip)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            for _ in range(30):
                response = await client.post("/api/v1/auth/registrations", content=b"not-json")
                assert response.status_code == 422
            login = await client.post(
                "/api/v1/auth/login",
                content=b"not-json",
                headers={"Origin": "https://app.example.net"},
            )
            assert login.status_code == 429
            assert login.headers["retry-after"]


async def test_registration_rejects_email_created_elsewhere_preserving_existing_account_and_session(
    registration_engine, monkeypatch, tmp_path
):
    from httpx import ASGITransport, AsyncClient

    from mdm.application.users import NewUser
    from mdm.domain.auth import UserRole, UserStatus
    from mdm.domain.credentials import RefreshToken, generate_opaque_token
    from mdm.infrastructure.repositories.sessions import SqlAlchemySessionTransaction
    from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository
    from mdm.main import create_app

    sender = InMemoryEmailSender(public_app_base_url="https://app.example.net")
    configure_http_registration(monkeypatch, tmp_path, sender)
    app = create_app()
    factory = create_session_factory(registration_engine)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="https://app.example.net"
        ) as client:
            assert (
                await client.post("/api/v1/auth/registrations", json={"email": EMAIL.value})
            ).status_code == 202
            async with factory.begin() as session:
                existing = await SqlAlchemyUserRepository(session).add(
                    NewUser(
                        EMAIL,
                        DisplayName("기존 관리자"),
                        "original-password-hash",
                        UserRole.SUPER_ADMIN,
                        UserStatus.ACTIVE,
                    )
                )
                tx = SqlAlchemySessionTransaction(session, existing)
                now = await tx.now()
                family = await tx.create_family(now, now + timedelta(days=7))
                refresh = generate_opaque_token(RefreshToken)
                await tx.add_token(family, refresh, now)
            response = await client.post(
                "/api/v1/auth/registrations/complete",
                headers={"Origin": "https://app.example.net"},
                json={
                    "token": emailed_token(sender).reveal(),
                    "name": NAME.value,
                    "password": PASSWORD.reveal(),
                },
            )
            assert response.status_code == 400
            assert response.json()["code"] == "INVALID_REGISTRATION_TOKEN"
            assert "set-cookie" not in response.headers
    async with factory() as session:
        users = (await session.scalars(select(UserRecord))).all()
        assert len(users) == 1
        user = users[0]
        assert (user.id, user.name, user.password_hash, user.role, user.updated_at) == (
            existing.id,
            existing.name.value,
            existing.password_hash,
            existing.role.value,
            existing.updated_at,
        )
        families = (await session.scalars(select(RefreshFamilyRecord))).all()
        assert len(families) == 1 and families[0].id == family and families[0].status == "ACTIVE"
        assert await session.scalar(select(RegistrationChallengeRecord.status)) == "ACTIVE"
        assert await session.scalar(select(RegistrationTokenRecord.used_at)) is None
