from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mdm.application.auth import EncodedAccessToken, PasswordHashUnavailable
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, User, UserRole, UserStatus
from mdm.domain.credentials import RegistrationToken, generate_opaque_token

pytestmark = pytest.mark.unit
NOW = datetime(2033, 1, 1, tzinfo=UTC)
EMAIL = EmailAddress("person@example.net")
PASSWORD = PlainPassword("valid password here")
NAME = DisplayName("가입 사용자")


def completion_fixture():
    from mdm.application.registrations import (
        CompleteRegistration,
        RegistrationChallenge,
        RegistrationTarget,
        StoredRegistrationToken,
    )

    token = generate_opaque_token(RegistrationToken)
    challenge = RegistrationChallenge(uuid4(), EMAIL, "ACTIVE", NOW + timedelta(days=1))
    target = RegistrationTarget(challenge.id, uuid4(), EMAIL)
    stored = StoredRegistrationToken(target.token_id, challenge.id, token.digest(), None)
    trace = []
    user = User(uuid4(), EMAIL, NAME, "hash", UserRole.USER, UserStatus.ACTIVE, NOW, NOW)
    tx = Mock()
    tx.lock_challenge = AsyncMock(return_value=challenge)
    tx.lock_token = AsyncMock(return_value=stored)
    tx.now = AsyncMock(return_value=NOW)
    tx.create_user = AsyncMock(return_value=user)
    tx.create_family = AsyncMock(return_value=uuid4())
    tx.add_refresh_token = AsyncMock()
    tx.complete = AsyncMock()

    @asynccontextmanager
    async def transaction(email):
        assert email == EMAIL
        trace.append("begin")
        try:
            yield tx
        except Exception:
            trace.append("rollback")
            raise
        else:
            trace.append("commit")

    repository = Mock()
    repository.find_token = AsyncMock(return_value=target)
    repository.transaction = transaction

    async def hash_password(password):
        assert password == PASSWORD
        assert "begin" not in trace
        trace.append("hash")
        return "hash"

    hasher = Mock()
    hasher.hash = AsyncMock(side_effect=hash_password)
    signer = Mock()
    signer.issue.return_value = EncodedAccessToken("test-access")
    service = CompleteRegistration(repository, hasher, signer, allowed_domains=("example.net",))
    return service, repository, tx, hasher, signer, trace, token, target, challenge, stored


async def test_registration_hashes_without_transaction_then_commits_user_and_session_together():
    service, _, tx, hasher, signer, trace, token, _, _, _ = completion_fixture()
    grant = await service.execute(token, NAME, PASSWORD)
    assert trace == ["hash", "begin", "commit"]
    hasher.hash.assert_awaited_once()
    tx.create_user.assert_awaited_once()
    new_user = tx.create_user.call_args.args[0]
    assert (new_user.role, new_user.status) == (UserRole.USER, UserStatus.ACTIVE)
    tx.complete.assert_awaited_once()
    assert grant.expires_at == NOW + timedelta(days=7)
    assert grant.issued_at == NOW
    assert grant.access_token.reveal() == "test-access"
    signer.issue.assert_called_once()


@pytest.mark.parametrize(
    "change", ["expired", "completed", "used", "digest", "missing", "membership"]
)
async def test_registration_rechecks_token_under_locks_after_hashing(change):
    from mdm.application.registrations import InvalidRegistrationToken

    service, _, tx, _, _, trace, token, _, challenge, stored = completion_fixture()
    if change == "expired":
        tx.now.return_value = challenge.expires_at
    elif change == "completed":
        tx.lock_challenge.return_value = replace(challenge, status="COMPLETED")
    elif change == "used":
        tx.lock_token.return_value = replace(stored, used_at=NOW)
    elif change == "digest":
        tx.lock_token.return_value = replace(stored, digest=b"x" * 32)
    elif change == "membership":
        tx.lock_token.return_value = replace(stored, challenge_id=uuid4())
    else:
        tx.lock_token.return_value = None
    with pytest.raises(InvalidRegistrationToken):
        await service.execute(token, NAME, PASSWORD)
    assert trace == ["hash", "begin", "rollback"]
    tx.create_user.assert_not_called()
    tx.complete.assert_not_called()


async def test_registration_hash_overload_never_opens_a_write_transaction():
    service, _, tx, hasher, _, trace, token, _, _, _ = completion_fixture()
    hasher.hash.side_effect = PasswordHashUnavailable
    with pytest.raises(PasswordHashUnavailable):
        await service.execute(token, NAME, PASSWORD)
    assert trace == []
    tx.create_user.assert_not_called()


async def test_registration_rejects_removed_domain_without_hashing_or_consuming_token():
    from mdm.application.registrations import CompleteRegistration, EmailDomainNotAllowed

    _, repository, tx, hasher, signer, trace, token, _, _, _ = completion_fixture()
    service = CompleteRegistration(repository, hasher, signer, allowed_domains=())
    with pytest.raises(EmailDomainNotAllowed):
        await service.execute(token, NAME, PASSWORD)
    assert trace == []
    hasher.hash.assert_not_called()
    tx.complete.assert_not_called()


async def test_registration_signing_failure_rolls_back_all_mutations():
    from mdm.application.registrations import RegistrationUnavailable

    service, _, _, _, signer, trace, token, _, _, _ = completion_fixture()
    signer.issue.side_effect = ValueError("private diagnostic")
    with pytest.raises(RegistrationUnavailable) as exc:
        await service.execute(token, NAME, PASSWORD)
    assert "private diagnostic" not in str(exc.value)
    assert trace == ["hash", "begin", "rollback"]


def request_fixture(*, exists=False, delivery="SENT"):
    from mdm.application.email_delivery import EmailDeliveryResult, EmailDeliveryStatus
    from mdm.application.registrations import RegistrationChallenge, RequestRegistration

    trace = []
    tx = Mock()
    tx.user_exists = AsyncMock(return_value=exists)
    tx.lock_active_challenge = AsyncMock(return_value=None)
    tx.now = AsyncMock(return_value=NOW)
    tx.expire = AsyncMock()
    tx.create_challenge = AsyncMock(
        return_value=RegistrationChallenge(uuid4(), EMAIL, "ACTIVE", NOW + timedelta(days=1))
    )
    tx.add_registration_token = AsyncMock()

    @asynccontextmanager
    async def transaction(email):
        trace.append("begin")
        try:
            yield tx
        except Exception:
            trace.append("rollback")
            raise
        else:
            trace.append("commit")
        finally:
            trace.append("closed")

    repository = Mock()
    repository.transaction = transaction

    async def send(message):
        assert trace[-2:] == ["commit", "closed"]
        trace.append("send")
        return EmailDeliveryResult(EmailDeliveryStatus(delivery))

    sender = Mock()
    sender.send = AsyncMock(side_effect=send)
    sink = Mock()
    service = RequestRegistration(
        repository, sender, sink, allowed_domains=("example.net",), clock=lambda: NOW
    )
    return service, tx, sender, sink, trace


@pytest.mark.parametrize("delivery", ["SENT", "REJECTED", "TIMED_OUT"])
async def test_registration_sends_only_after_commit_and_close_and_records_failure_once(delivery):
    service, tx, sender, sink, trace = request_fixture(delivery=delivery)
    await service.execute(EMAIL)
    assert trace == ["begin", "commit", "closed", "send"]
    tx.create_challenge.assert_awaited_once_with(NOW, NOW + timedelta(days=1))
    message = sender.send.call_args.args[0]
    assert message.recipient == EMAIL
    assert message.message_type.value == "REGISTRATION"
    assert isinstance(message.token, RegistrationToken)
    if delivery == "SENT":
        sink.emit.assert_not_called()
    else:
        sink.emit.assert_called_once()
        event = sink.emit.call_args.args[0]
        assert event.name == "EMAIL_DELIVERY_FAILED"
        assert EMAIL.value not in repr(event)
        assert message.token.reveal() not in repr(event)


async def test_registration_repeated_request_keeps_original_deadline_and_adds_token():
    from mdm.application.registrations import RegistrationChallenge

    service, tx, _, _, _ = request_fixture()
    challenge = RegistrationChallenge(uuid4(), EMAIL, "ACTIVE", NOW + timedelta(minutes=1))
    tx.lock_active_challenge.return_value = challenge
    await service.execute(EMAIL)
    tx.create_challenge.assert_not_called()
    tx.expire.assert_not_called()
    assert tx.add_registration_token.call_args.args[0] == challenge.id


async def test_registration_request_replaces_challenge_at_exact_expiry():
    from mdm.application.registrations import RegistrationChallenge

    service, tx, _, _, _ = request_fixture()
    challenge = RegistrationChallenge(uuid4(), EMAIL, "ACTIVE", NOW)
    tx.lock_active_challenge.return_value = challenge
    await service.execute(EMAIL)
    tx.expire.assert_awaited_once_with(challenge.id)
    tx.create_challenge.assert_awaited_once_with(NOW, NOW + timedelta(days=1))


async def test_registration_existing_user_does_not_create_credentials_or_send_email():
    service, tx, sender, sink, trace = request_fixture(exists=True)
    await service.execute(EMAIL)
    assert trace == ["begin", "commit", "closed"]
    tx.create_challenge.assert_not_called()
    tx.add_registration_token.assert_not_called()
    sender.send.assert_not_called()
    sink.emit.assert_not_called()


async def test_registration_domain_rejection_precedes_account_lookup():
    from mdm.application.registrations import EmailDomainNotAllowed

    service, tx, _, _, trace = request_fixture(exists=True)
    with pytest.raises(EmailDomainNotAllowed):
        await service.execute(EmailAddress("person@sub.example.net"))
    assert trace == []
    tx.user_exists.assert_not_called()


@pytest.mark.parametrize("count", [1, 4])
async def test_registration_digest_collision_retries_or_rolls_back_without_email(count):
    from mdm.application.registrations import RegistrationDigestConflict
    from mdm.application.tokens import OpaqueTokenCollision

    service, tx, sender, _, trace = request_fixture()
    tx.add_registration_token.side_effect = [RegistrationDigestConflict] * count + [None]
    if count == 4:
        with pytest.raises(OpaqueTokenCollision):
            await service.execute(EMAIL)
        assert trace == ["begin", "rollback", "closed"]
        sender.send.assert_not_called()
    else:
        await service.execute(EMAIL)
    assert tx.add_registration_token.call_count == min(4, count + 1)


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=24))
def test_exact_registration_domain_policy_never_accepts_suffix_or_subdomain_matches(label):
    from mdm.application.registrations import EmailDomainNotAllowed, require_allowed_domain

    allowed = label + ".example.net"
    require_allowed_domain(EmailAddress("person@" + allowed.upper()), (allowed,))
    for domain in ("sub." + allowed, allowed + ".another.net", "prefix-" + allowed):
        with pytest.raises(EmailDomainNotAllowed):
            require_allowed_domain(EmailAddress("person@" + domain), (allowed,))


async def test_registration_existing_email_conflict_rolls_back_without_session_changes():
    from mdm.application.registrations import InvalidRegistrationToken
    from mdm.application.users import DuplicateUserEmail

    service, _, tx, _, signer, trace, token, _, _, _ = completion_fixture()
    tx.create_user.side_effect = DuplicateUserEmail("normalized email already exists")
    with pytest.raises(InvalidRegistrationToken):
        await service.execute(token, NAME, PASSWORD)
    assert trace == ["hash", "begin", "rollback"]
    tx.create_family.assert_not_called()
    tx.complete.assert_not_called()
    signer.issue.assert_not_called()
