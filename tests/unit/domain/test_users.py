from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from mdm.application.users import NewUser, NewUserSecurityEvent
from mdm.domain.auth import (
    AuthInvariantError,
    DisplayName,
    EmailAddress,
    SecurityEventInitiatorType,
    SecurityEventType,
    User,
    UserRole,
    UserSecurityEvent,
    UserStatus,
)

SUBJECT_ID = UUID("018f3f0e-7b2a-7e8f-9f62-9876543210ab")
INITIATOR_ID = UUID("018f3f0e-7b2a-7e8f-9f62-0123456789ab")
EVENT_ID = UUID("018f3f0e-7b2a-7e8f-9f62-abcdefabcdef")
NOW = datetime(2026, 9, 9, tzinfo=UTC)


@pytest.mark.unit
def test_user_snapshot_keeps_exact_single_role_and_hides_password_hash() -> None:
    user = User(
        id=SUBJECT_ID,
        email=EmailAddress("Admin@Example.net"),
        name=DisplayName("관리자"),
        password_hash="$argon2id$secret-material",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )

    assert user.email.value == "admin@example.net"
    assert user.role is UserRole.SUPER_ADMIN
    assert "$argon2id$secret-material" not in repr(user)


@pytest.mark.unit
def test_new_user_does_not_leak_password_hash_through_repr() -> None:
    new_user = NewUser(
        email=EmailAddress("admin@example.net"),
        name=DisplayName("관리자"),
        password_hash="$argon2id$secret-material",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )

    assert "$argon2id$secret-material" not in repr(new_user)


@pytest.mark.unit
def test_user_snapshot_requires_timezone_aware_ordered_timestamps() -> None:
    values = {
        "id": SUBJECT_ID,
        "email": EmailAddress("admin@example.net"),
        "name": DisplayName("관리자"),
        "password_hash": "hash",
        "role": UserRole.ADMIN,
        "status": UserStatus.ACTIVE,
    }

    with pytest.raises(AuthInvariantError):
        User(**values, created_at=NOW.replace(tzinfo=None), updated_at=NOW)
    with pytest.raises(AuthInvariantError):
        User(**values, created_at=NOW, updated_at=NOW.replace(year=2025))


def _event(**changes: object) -> UserSecurityEvent:
    values: dict[str, object] = {
        "id": EVENT_ID,
        "event_type": SecurityEventType.USER_ROLE_CHANGED,
        "subject_user_id": SUBJECT_ID,
        "occurred_at": NOW,
        "initiator_type": SecurityEventInitiatorType.AUTHENTICATED_USER,
        "initiator_user_id": INITIATOR_ID,
        "initiator_role": UserRole.SUPER_ADMIN,
        "previous_role": UserRole.USER,
        "new_role": UserRole.ADMIN,
    }
    values.update(changes)
    return UserSecurityEvent(**cast(Any, values))


@pytest.mark.unit
@pytest.mark.parametrize(
    "event",
    [
        _event(),
        _event(
            event_type=SecurityEventType.USER_DISABLED,
            previous_role=None,
            new_role=None,
            previous_status=UserStatus.ACTIVE,
            new_status=UserStatus.DISABLED,
        ),
        _event(
            event_type=SecurityEventType.USER_ENABLED,
            previous_role=None,
            new_role=None,
            previous_status=UserStatus.DISABLED,
            new_status=UserStatus.ACTIVE,
        ),
        _event(
            event_type=SecurityEventType.PASSWORD_CHANGED,
            previous_role=None,
            new_role=None,
        ),
        _event(
            event_type=SecurityEventType.PASSWORD_RESET,
            initiator_type=SecurityEventInitiatorType.RESET_TOKEN,
            initiator_user_id=None,
            initiator_role=None,
            previous_role=None,
            new_role=None,
        ),
        _event(
            event_type=SecurityEventType.INITIAL_SUPER_ADMIN_BOOTSTRAPPED,
            initiator_type=SecurityEventInitiatorType.BOOTSTRAP_CLI,
            initiator_user_id=None,
            initiator_role=None,
            previous_role=None,
            new_role=UserRole.SUPER_ADMIN,
            new_status=UserStatus.ACTIVE,
        ),
    ],
)
def test_security_event_accepts_each_contract_shape(event: UserSecurityEvent) -> None:
    assert event.subject_user_id == SUBJECT_ID


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [
        {"initiator_user_id": None},
        {"initiator_role": None},
        {"previous_role": UserRole.USER, "new_role": UserRole.USER},
        {"previous_status": UserStatus.ACTIVE},
        {
            "event_type": SecurityEventType.PASSWORD_RESET,
            "previous_role": None,
            "new_role": None,
        },
        {
            "event_type": SecurityEventType.INITIAL_SUPER_ADMIN_BOOTSTRAPPED,
            "initiator_type": SecurityEventInitiatorType.BOOTSTRAP_CLI,
            "initiator_user_id": None,
            "initiator_role": None,
            "previous_role": None,
            "new_role": UserRole.ADMIN,
            "new_status": UserStatus.ACTIVE,
        },
    ],
)
def test_security_event_rejects_crossed_initiator_or_transition_shapes(
    changes: dict[str, object],
) -> None:
    with pytest.raises(AuthInvariantError):
        _event(**changes)


@pytest.mark.unit
def test_new_security_event_rejects_invalid_shape_before_persistence() -> None:
    with pytest.raises(AuthInvariantError):
        NewUserSecurityEvent(
            event_type=SecurityEventType.PASSWORD_RESET,
            subject_user_id=SUBJECT_ID,
            initiator_type=SecurityEventInitiatorType.AUTHENTICATED_USER,
            initiator_user_id=INITIATOR_ID,
            initiator_role=UserRole.ADMIN,
        )
