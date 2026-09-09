import pytest

from mdm.domain.auth import (
    AuthInvariantError,
    DisplayName,
    EmailAddress,
    PlainPassword,
    UserRole,
    UserStatus,
    normalize_email,
)


@pytest.mark.unit
def test_email_normalizes_whitespace_unicode_local_part_and_idna_domain() -> None:
    email = EmailAddress("  TÉST@BÜCHER.de  ")

    assert email.value == "tést@xn--bcher-kva.de"


@pytest.mark.unit
def test_email_normalization_is_idempotent() -> None:
    normalized = EmailAddress("User+Tag@Example.net").value

    assert EmailAddress(normalized).value == normalized
    assert normalize_email("User+Tag@Example.net") == normalized


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["missing-at.example.net", "a@@example.net", "a b@example.net", "a@localhost"],
)
def test_email_rejects_nonstandard_formats(value: str) -> None:
    with pytest.raises(AuthInvariantError):
        EmailAddress(value)


@pytest.mark.unit
def test_email_accepts_254_characters_and_rejects_255() -> None:
    local = "a" * 64
    domain_189 = ".".join(("b" * 63, "c" * 63, "d" * 61))
    domain_190 = ".".join(("b" * 63, "c" * 63, "d" * 62))

    assert len(EmailAddress(f"{local}@{domain_189}").value) == 254
    with pytest.raises(AuthInvariantError):
        EmailAddress(f"{local}@{domain_190}")


@pytest.mark.unit
def test_display_name_normalizes_nfc_and_outer_unicode_whitespace() -> None:
    name = DisplayName(" \tHong Gildong  \u1100\u1161\n")

    assert name.value == "Hong Gildong  가"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "   ", "a" * 101, "valid\u0000name"])
def test_display_name_rejects_invalid_values(value: str) -> None:
    with pytest.raises(AuthInvariantError):
        DisplayName(value)


@pytest.mark.unit
def test_display_name_preserves_internal_content() -> None:
    name = DisplayName("  Kim  O'Neil-김🙂  ")

    assert name.value == "Kim  O'Neil-김🙂"


@pytest.mark.unit
def test_password_normalizes_nfc_without_trimming_or_casefolding() -> None:
    password = PlainPassword("  A\u1100\u1161 password  ")

    assert password.reveal() == "  A가 password  "
    assert password.reveal().startswith("  A")


@pytest.mark.unit
@pytest.mark.parametrize("value", ["a" * 14, "a" * 129])
def test_password_rejects_values_outside_code_point_bounds(value: str) -> None:
    with pytest.raises(AuthInvariantError):
        PlainPassword(value)


@pytest.mark.unit
def test_password_does_not_leak_through_repr() -> None:
    password = PlainPassword("correct horse battery staple")

    assert "correct horse" not in repr(password)


@pytest.mark.unit
def test_roles_and_statuses_are_exact() -> None:
    assert [role.value for role in UserRole] == ["USER", "ADMIN", "SUPER_ADMIN"]
    assert [status.value for status in UserStatus] == ["ACTIVE", "DISABLED"]
