from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mdm.application.auth import HumanPrincipal, PasswordHashUnavailable
from mdm.application.sessions import (
    InvalidSession,
    RefreshFamily,
    RefreshTarget,
    StoredRefreshToken,
)
from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, User, UserRole, UserStatus
from mdm.domain.credentials import PasswordResetToken, RefreshToken, generate_opaque_token

pytestmark = pytest.mark.unit
NOW = datetime(2033, 1, 1, tzinfo=UTC)
PASSWORD = PlainPassword("valid current password")
NEW_PASSWORD = PlainPassword("valid replacement password")


def fixture():
    from mdm.application.password_lifecycle import (
        PasswordLifecycle,
        ResetChallenge,
        ResetTarget,
        StoredResetToken,
    )

    user = User(
        uuid4(),
        EmailAddress("person@example.net"),
        DisplayName("사용자"),
        "old-hash",
        UserRole.USER,
        UserStatus.ACTIVE,
        NOW,
        NOW,
    )
    refresh = generate_opaque_token(RefreshToken)
    reset = generate_opaque_token(PasswordResetToken)
    family = RefreshFamily(uuid4(), user.id, "ACTIVE", NOW + timedelta(days=7))
    stored_refresh = StoredRefreshToken(uuid4(), family.id, refresh.digest(), None)
    challenge = ResetChallenge(uuid4(), user.id, "ACTIVE", NOW + timedelta(minutes=30))
    stored_reset = StoredResetToken(uuid4(), challenge.id, reset.digest(), None)
    tx = Mock(user=user)
    tx.lock_families = AsyncMock(return_value=(family,))
    tx.lock_refresh_tokens = AsyncMock(return_value=(stored_refresh,))
    tx.lock_challenges = AsyncMock(return_value=(challenge,))
    tx.lock_reset_tokens = AsyncMock(return_value=(stored_reset,))
    tx.now = AsyncMock(return_value=NOW)
    for name in ("revoke", "change_hash", "complete_reset", "revoke_challenge", "audit_password"):
        setattr(tx, name, AsyncMock())
    tx.create_family = AsyncMock(return_value=uuid4())
    tx.add_token = AsyncMock(return_value=uuid4())
    active = False
    trace = []

    @asynccontextmanager
    async def transaction(user_id):
        nonlocal active
        assert user_id == user.id
        active = True
        trace.append("begin")
        try:
            yield tx
        except Exception:
            trace.append("rollback")
            raise
        else:
            trace.append("commit")
        finally:
            active = False

    repo = Mock()
    repo.get_by_id = AsyncMock(return_value=user)
    repo.get_by_email = AsyncMock(return_value=user)
    repo.find_refresh = AsyncMock(return_value=RefreshTarget(user.id, family.id, stored_refresh.id))
    repo.find_reset = AsyncMock(return_value=ResetTarget(user.id, challenge.id, stored_reset.id))
    repo.transaction = transaction

    async def verify(password, encoded):
        assert not active
        trace.append("verify")
        return True

    async def hash_password(password):
        assert not active
        trace.append("hash")
        return "new-hash"

    hasher = Mock(hash=AsyncMock(side_effect=hash_password), verify=AsyncMock(side_effect=verify))
    events = Mock()
    service = PasswordLifecycle(repo, hasher, events, clock=lambda: NOW)
    return service, repo, tx, hasher, events, user, refresh, reset, trace


async def test_reset_changes_password_revokes_sessions_and_audits_in_one_transaction():
    service, _, tx, hasher, _, _, _, reset, trace = fixture()
    await service.complete_reset(reset, NEW_PASSWORD)
    assert trace == ["hash", "begin", "commit"]
    hasher.verify.assert_not_called()
    tx.change_hash.assert_awaited_once_with("new-hash", NOW)
    tx.revoke.assert_awaited_once()
    tx.complete_reset.assert_awaited_once()
    tx.audit_password.assert_awaited_once_with(None, NOW)
    names = [call[0] for call in tx.mock_calls]
    assert names.index("lock_families") < names.index("lock_refresh_tokens")
    assert names.index("lock_refresh_tokens") < names.index("lock_challenges")
    assert names.index("lock_challenges") < names.index("lock_reset_tokens")


async def test_password_change_verifies_outside_transactions_and_rotates_only_after_rechecking():
    service, _, tx, _, _, user, refresh, _, trace = fixture()
    principal = HumanPrincipal(user.id, UserRole.ADMIN)
    grant = await service.change_password(principal, refresh, PASSWORD, NEW_PASSWORD)
    assert trace == ["begin", "commit", "verify", "hash", "begin", "commit"]
    assert grant.expires_at == NOW + timedelta(days=7)
    tx.change_hash.assert_awaited_once_with("new-hash", NOW)
    tx.revoke_challenge.assert_awaited_once()
    tx.audit_password.assert_awaited_once_with(principal, NOW)


async def test_cross_user_refresh_is_rejected_before_hashing_and_without_transaction():
    service, _, tx, hasher, events, _, refresh, _, trace = fixture()
    with pytest.raises(InvalidSession):
        await service.change_password(
            HumanPrincipal(uuid4(), UserRole.USER), refresh, PASSWORD, NEW_PASSWORD
        )
    assert trace == []
    hasher.verify.assert_not_called()
    hasher.hash.assert_not_called()
    tx.change_hash.assert_not_called()
    events.emit.assert_called_once()
    assert events.emit.call_args.args[0].name == "SESSION_PRINCIPAL_MISMATCH"


@pytest.mark.parametrize("seconds", [0, 5, 10, 11, 100])
async def test_used_refresh_never_changes_database_even_during_rotation_grace(seconds):
    service, _, tx, hasher, events, user, refresh, _, trace = fixture()
    stored = tx.lock_refresh_tokens.return_value[0]
    tx.lock_refresh_tokens.return_value = (
        replace(stored, used_at=NOW - timedelta(seconds=seconds)),
    )
    with pytest.raises(InvalidSession):
        await service.change_password(
            HumanPrincipal(user.id, user.role), refresh, PASSWORD, NEW_PASSWORD
        )
    assert trace == ["begin", "rollback"]
    hasher.verify.assert_not_called()
    tx.revoke.assert_not_called()
    tx.change_hash.assert_not_called()
    events.emit.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["expired", "completed", "used", "digest", "membership", "disabled", "hash"]
)
async def test_reset_rechecks_credentials_and_user_under_lock_after_hashing(kind):
    from mdm.application.password_lifecycle import InvalidPasswordResetToken

    service, _, tx, _, _, _, _, reset, trace = fixture()
    challenge = tx.lock_challenges.return_value[0]
    stored = tx.lock_reset_tokens.return_value[0]
    if kind == "expired":
        tx.now.return_value = challenge.expires_at
    elif kind == "completed":
        tx.lock_challenges.return_value = (replace(challenge, status="COMPLETED"),)
    elif kind == "used":
        tx.lock_reset_tokens.return_value = (replace(stored, used_at=NOW),)
    elif kind == "digest":
        tx.lock_reset_tokens.return_value = (replace(stored, digest=b"x" * 32),)
    elif kind == "membership":
        tx.lock_challenges.return_value = (replace(challenge, user_id=uuid4()),)
    elif kind == "disabled":
        tx.user = replace(tx.user, status=UserStatus.DISABLED)
    else:
        tx.user = replace(tx.user, password_hash="changed-by-another-request")
    with pytest.raises(InvalidPasswordResetToken):
        await service.complete_reset(reset, NEW_PASSWORD)
    assert trace == ["hash", "begin", "rollback"]
    tx.change_hash.assert_not_called()
    tx.revoke.assert_not_called()


async def test_hash_overload_does_not_consume_reset():
    service, _, tx, hasher, _, _, _, reset, trace = fixture()
    hasher.hash.side_effect = PasswordHashUnavailable
    with pytest.raises(PasswordHashUnavailable):
        await service.complete_reset(reset, NEW_PASSWORD)
    assert trace == []
    tx.complete_reset.assert_not_called()


async def test_change_rechecks_session_after_hashing_without_revoking_on_failure():
    service, _, tx, hasher, _, user, refresh, _, trace = fixture()
    original_hash = hasher.hash.side_effect

    async def rotate_while_hashing(password):
        result = await original_hash(password)
        tx.lock_refresh_tokens.return_value = (
            replace(tx.lock_refresh_tokens.return_value[0], used_at=NOW),
        )
        return result

    hasher.hash.side_effect = rotate_while_hashing
    with pytest.raises(InvalidSession):
        await service.change_password(
            HumanPrincipal(user.id, user.role), refresh, PASSWORD, NEW_PASSWORD
        )
    assert trace == ["begin", "commit", "verify", "hash", "begin", "rollback"]
    tx.change_hash.assert_not_called()
    tx.revoke.assert_not_called()


async def test_mismatch_logging_failure_does_not_change_authentication_result():
    service, _, tx, hasher, events, _, refresh, _, trace = fixture()
    events.emit.side_effect = OSError("unavailable log sink")
    with pytest.raises(InvalidSession):
        await service.change_password(
            HumanPrincipal(uuid4(), UserRole.USER), refresh, PASSWORD, NEW_PASSWORD
        )
    assert trace == []
    hasher.verify.assert_not_called()
    tx.revoke.assert_not_called()


@given(seconds=st.integers(min_value=-1800, max_value=1800))
async def test_reset_expiry_boundary_is_inclusive_and_never_mutates_expired_tokens(seconds):
    from mdm.application.password_lifecycle import InvalidPasswordResetToken

    service, _, tx, _, _, _, _, reset, _ = fixture()
    deadline = tx.lock_challenges.return_value[0].expires_at
    tx.now.return_value = deadline + timedelta(seconds=seconds)
    if seconds >= 0:
        with pytest.raises(InvalidPasswordResetToken):
            await service.complete_reset(reset, NEW_PASSWORD)
        tx.change_hash.assert_not_called()
        tx.complete_reset.assert_not_called()
    else:
        await service.complete_reset(reset, NEW_PASSWORD)
        tx.complete_reset.assert_awaited_once()
