from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from mdm.domain.auth import DisplayName, EmailAddress, PlainPassword, User, UserRole, UserStatus

NOW = datetime(2033, 1, 1, tzinfo=UTC)
USER = User(
    id=UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab"),
    email=EmailAddress("person@example.net"),
    name=DisplayName("사용자"),
    password_hash="stored-hash",
    role=UserRole.USER,
    status=UserStatus.ACTIVE,
    created_at=NOW,
    updated_at=NOW,
)


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["missing", "wrong", "disabled"])
async def test_login_failures_verify_password_and_emit_once_without_mutation(kind: str) -> None:
    from mdm.application.sessions import InvalidCredentials, SessionUseCases

    user = None if kind == "missing" else USER
    if kind == "disabled":
        user = replace(USER, status=UserStatus.DISABLED)
    repository = Mock()
    repository.get_by_email = AsyncMock(return_value=user)
    hasher = Mock()
    hasher.verify = AsyncMock(return_value=kind != "wrong")
    sink = Mock()
    use_cases = SessionUseCases(
        repository, hasher, Mock(), sink, fake_password_hash="fake-hash", clock=lambda: NOW
    )
    with pytest.raises(InvalidCredentials):
        await use_cases.login(
            EmailAddress("person@example.net"), PlainPassword("valid password here")
        )

    hasher.verify.assert_awaited_once()
    assert hasher.verify.call_args.args[1] == ("fake-hash" if kind == "missing" else "stored-hash")
    repository.transaction.assert_not_called()
    sink.emit.assert_called_once()
    assert sink.emit.call_args.args[0].name == "LOGIN_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize("change", ["password", "status"])
async def test_login_rechecks_locked_user_after_connection_free_password_verification(
    change: str,
) -> None:
    from mdm.application.sessions import InvalidCredentials, SessionUseCases

    transaction_active = False
    changed = replace(USER, password_hash="changed-hash")
    if change == "status":
        changed = replace(USER, status=UserStatus.DISABLED)
    tx = Mock()
    tx.user = changed

    @asynccontextmanager
    async def transaction(user_id: UUID):
        nonlocal transaction_active
        assert user_id == USER.id
        transaction_active = True
        try:
            yield tx
        finally:
            transaction_active = False

    repository = Mock()
    repository.get_by_email = AsyncMock(return_value=USER)
    repository.transaction = transaction

    async def verify(password: PlainPassword, encoded_hash: str) -> bool:
        assert not transaction_active
        assert encoded_hash == USER.password_hash
        return True

    hasher = Mock()
    hasher.verify = verify
    sink = Mock()
    use_cases = SessionUseCases(
        repository, hasher, Mock(), sink, fake_password_hash="fake-hash", clock=lambda: NOW
    )
    with pytest.raises(InvalidCredentials):
        await use_cases.login(USER.email, PlainPassword("valid password here"))
    tx.create_family.assert_not_called()
    sink.emit.assert_called_once()


@pytest.mark.unit
async def test_profile_combines_current_database_fields_with_jwt_role() -> None:
    from mdm.application.auth import HumanPrincipal
    from mdm.application.sessions import GetCurrentProfile

    repository = Mock()
    repository.get_by_id = AsyncMock(return_value=replace(USER, role=UserRole.ADMIN))
    profile = await GetCurrentProfile(repository).execute(HumanPrincipal(USER.id, UserRole.USER))
    assert profile.email == USER.email.value
    assert profile.name == USER.name.value
    assert profile.effective_role == "USER"


@pytest.mark.unit
async def test_password_capacity_failure_is_not_recorded_as_login_failure():
    from mdm.application.auth import PasswordHashUnavailable
    from mdm.application.sessions import SessionUseCases

    repository = Mock()
    repository.get_by_email = AsyncMock(return_value=USER)
    hasher = Mock()
    hasher.verify = AsyncMock(side_effect=PasswordHashUnavailable)
    sink = Mock()
    use_cases = SessionUseCases(
        repository, hasher, Mock(), sink, fake_password_hash="fake-hash", clock=lambda: NOW
    )
    with pytest.raises(PasswordHashUnavailable):
        await use_cases.login(USER.email, PlainPassword("valid password here"))
    repository.transaction.assert_not_called()
    sink.emit.assert_not_called()
