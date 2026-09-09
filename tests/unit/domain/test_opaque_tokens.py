import base64
import hashlib

import pytest

from mdm.application.tokens import OpaqueTokenCollision, issue_unique_opaque_token
from mdm.domain.credentials import (
    CsrfToken,
    InvalidOpaqueToken,
    PasswordResetToken,
    RefreshToken,
    RegistrationToken,
    csrf_tokens_match,
    generate_opaque_token,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "token_type",
    [RefreshToken, RegistrationToken, PasswordResetToken, CsrfToken],
)
def test_opaque_token_has_canonical_256_bit_wire_format(token_type: type[RefreshToken]) -> None:
    raw_bytes = bytes(range(32))

    token = generate_opaque_token(token_type, random_bytes=lambda _: raw_bytes)

    expected = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
    assert token.reveal() == expected
    assert len(token.reveal()) == 43
    assert token.digest() == hashlib.sha256(raw_bytes).digest()
    assert repr(token) == f"{token_type.__name__}(value=<redacted>)"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "a" * 42,
        "a" * 44,
        "a" * 42 + "=",
        "a" * 42 + "+",
        "a" * 42 + "/",
        "é" * 43,
    ],
)
def test_opaque_token_rejects_malformed_or_noncanonical_wire_values(value: str) -> None:
    with pytest.raises(InvalidOpaqueToken):
        RefreshToken(value)


@pytest.mark.unit
def test_token_purposes_are_not_interchangeable() -> None:
    refresh = generate_opaque_token(RefreshToken, random_bytes=lambda _: b"r" * 32)

    assert not isinstance(refresh, RegistrationToken)
    assert not isinstance(refresh, PasswordResetToken)
    assert not isinstance(refresh, CsrfToken)


@pytest.mark.unit
def test_csrf_tokens_use_an_explicit_equality_boundary() -> None:
    first = generate_opaque_token(CsrfToken, random_bytes=lambda _: b"a" * 32)
    same = CsrfToken(first.reveal())
    other = generate_opaque_token(CsrfToken, random_bytes=lambda _: b"b" * 32)

    assert csrf_tokens_match(first, same) is True
    assert csrf_tokens_match(first, other) is False


@pytest.mark.unit
async def test_unique_token_regenerates_at_most_three_times_after_collisions() -> None:
    samples = iter([b"a" * 32, b"b" * 32, b"c" * 32, b"d" * 32])
    colliding_digests = {hashlib.sha256(value * 32).digest() for value in (b"a", b"b", b"c")}

    async def digest_exists(digest: bytes) -> bool:
        return digest in colliding_digests

    token = await issue_unique_opaque_token(
        RegistrationToken,
        digest_exists=digest_exists,
        random_bytes=lambda _: next(samples),
    )

    assert token.digest() == hashlib.sha256(b"d" * 32).digest()


@pytest.mark.unit
async def test_unique_token_fails_safely_after_three_regenerations() -> None:
    raw = b"s" * 32

    async def digest_exists(_: bytes) -> bool:
        return True

    with pytest.raises(OpaqueTokenCollision) as raised:
        await issue_unique_opaque_token(
            PasswordResetToken,
            digest_exists=digest_exists,
            random_bytes=lambda _: raw,
        )

    assert base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") not in str(raised.value)
    assert hashlib.sha256(raw).hexdigest() not in str(raised.value)
