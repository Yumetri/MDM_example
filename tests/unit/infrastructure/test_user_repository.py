import traceback
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mdm.application.users import NewUser, UserPersistenceError
from mdm.domain.auth import DisplayName, EmailAddress, UserRole, UserStatus
from mdm.infrastructure.repositories.users import SqlAlchemyUserRepository


@pytest.mark.unit
async def test_user_repository_sanitizes_unexpected_integrity_error() -> None:
    secret_hash = "$argon2id$must-not-appear"
    original = IntegrityError(
        "INSERT INTO users (...)",
        {"password_hash": secret_hash},
        RuntimeError(f"failed row contains {secret_hash}"),
    )
    session = MagicMock()
    session.flush = AsyncMock(side_effect=original)
    repository = SqlAlchemyUserRepository(cast(AsyncSession, session))
    new_user = NewUser(
        email=EmailAddress("user@example.net"),
        name=DisplayName("사용자"),
        password_hash=secret_hash,
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )

    with pytest.raises(UserPersistenceError) as raised:
        await repository.add(new_user)

    displayed_traceback = "".join(traceback.format_exception(raised.value))
    assert secret_hash not in str(raised.value)
    assert secret_hash not in displayed_traceback
