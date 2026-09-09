from datetime import UTC, datetime
from uuid import UUID

import pytest

from mdm.application.auth import InvalidStoredPasswordHash, PasswordHasher
from mdm.application.bootstrap import (
    BootstrapOutcome,
    BootstrapStateConflict,
    BootstrapSuperAdmin,
    BootstrapSuperAdminRepository,
)
from mdm.application.users import DuplicateUserEmail, NewUser
from mdm.domain.auth import (
    DisplayName,
    EmailAddress,
    PlainPassword,
    User,
    UserRole,
    UserStatus,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _user(
    *,
    name: str = "기존 관리자",
    password_hash: str = "stored-hash",
    role: UserRole = UserRole.SUPER_ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    return User(
        id=UUID("0199-2f0f-b8aa-7000-8000-000000000001"),
        email=EmailAddress("admin@example.net"),
        name=DisplayName(name),
        password_hash=password_hash,
        role=role,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


class FakePasswordHasher(PasswordHasher):
    def __init__(
        self,
        *,
        password_matches: bool = True,
        invalid_stored_hash: bool = False,
    ) -> None:
        self.password_matches = password_matches
        self.invalid_stored_hash = invalid_stored_hash
        self.hash_calls = 0
        self.verify_calls = 0

    async def hash(self, password: PlainPassword) -> str:
        self.hash_calls += 1
        assert password.reveal() == "correct horse battery staple"
        return "new-hash"

    async def verify(self, password: PlainPassword, encoded_hash: str) -> bool:
        self.verify_calls += 1
        assert password.reveal() == "correct horse battery staple"
        assert encoded_hash in {"stored-hash", "new-hash"}
        if self.invalid_stored_hash:
            raise InvalidStoredPasswordHash("stored-hash")
        return self.password_matches


class FakeBootstrapRepository(BootstrapSuperAdminRepository):
    def __init__(
        self,
        *,
        existing: User | None = None,
        duplicate_on_create: bool = False,
    ) -> None:
        self.existing = existing
        self.duplicate_on_create = duplicate_on_create
        self.create_calls: list[NewUser] = []
        self.read_count = 0

    async def get_by_email(self, email: EmailAddress) -> User | None:
        self.read_count += 1
        assert email == EmailAddress("admin@example.net")
        return self.existing

    async def create_with_security_event(self, new_user: NewUser) -> User:
        self.create_calls.append(new_user)
        if self.duplicate_on_create:
            self.duplicate_on_create = False
            self.existing = _user(password_hash="new-hash")
            raise DuplicateUserEmail("normalized email already exists")
        self.existing = _user(name=new_user.name.value, password_hash=new_user.password_hash)
        return self.existing


@pytest.mark.unit
async def test_bootstrap_creates_active_super_admin_with_one_atomic_repository_call() -> None:
    repository = FakeBootstrapRepository()
    hasher = FakePasswordHasher()

    outcome = await BootstrapSuperAdmin(repository, hasher).execute(
        email="  ADMIN@Example.net ",
        name=" 최초 관리자 ",
        password="correct horse battery staple",
    )

    assert outcome is BootstrapOutcome.CREATED
    assert hasher.hash_calls == 1
    assert hasher.verify_calls == 0
    assert len(repository.create_calls) == 1
    assert repository.create_calls[0].email == EmailAddress("admin@example.net")
    assert repository.create_calls[0].name == DisplayName("최초 관리자")
    assert repository.create_calls[0].role is UserRole.SUPER_ADMIN
    assert repository.create_calls[0].status is UserStatus.ACTIVE


@pytest.mark.unit
async def test_bootstrap_repeated_match_is_no_op_and_ignores_name_difference() -> None:
    repository = FakeBootstrapRepository(existing=_user())
    hasher = FakePasswordHasher()

    outcome = await BootstrapSuperAdmin(repository, hasher).execute(
        email="admin@example.net",
        name="새 이름이지만 무시",
        password="correct horse battery staple",
    )

    assert outcome is BootstrapOutcome.NO_OP
    assert hasher.hash_calls == 0
    assert hasher.verify_calls == 1
    assert repository.create_calls == []
    assert repository.existing == _user()


@pytest.mark.unit
async def test_bootstrap_unique_race_rolls_forward_to_verified_no_op() -> None:
    repository = FakeBootstrapRepository(duplicate_on_create=True)
    hasher = FakePasswordHasher()

    outcome = await BootstrapSuperAdmin(repository, hasher).execute(
        email="admin@example.net",
        name="최초 관리자",
        password="correct horse battery staple",
    )

    assert outcome is BootstrapOutcome.NO_OP
    assert hasher.hash_calls == 1
    assert hasher.verify_calls == 1
    assert repository.read_count >= 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("existing", "password_matches"),
    [
        (_user(role=UserRole.ADMIN), True),
        (_user(status=UserStatus.DISABLED), True),
        (_user(), False),
    ],
)
async def test_bootstrap_mismatches_share_one_sanitized_conflict(
    existing: User,
    password_matches: bool,
) -> None:
    repository = FakeBootstrapRepository(existing=existing)
    hasher = FakePasswordHasher(password_matches=password_matches)

    with pytest.raises(BootstrapStateConflict, match=r"^BOOTSTRAP_STATE_CONFLICT$") as error:
        await BootstrapSuperAdmin(repository, hasher).execute(
            email="admin@example.net",
            name="최초 관리자",
            password="correct horse battery staple",
        )

    rendered = repr(error.value)
    assert "admin@example.net" not in rendered
    assert "correct horse battery staple" not in rendered
    assert "stored-hash" not in rendered


@pytest.mark.unit
async def test_bootstrap_invalid_stored_hash_is_the_same_state_conflict() -> None:
    repository = FakeBootstrapRepository(existing=_user())
    hasher = FakePasswordHasher(invalid_stored_hash=True)

    with pytest.raises(BootstrapStateConflict, match=r"^BOOTSTRAP_STATE_CONFLICT$"):
        await BootstrapSuperAdmin(repository, hasher).execute(
            email="admin@example.net",
            name="최초 관리자",
            password="correct horse battery staple",
        )
