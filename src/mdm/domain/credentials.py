"""Purpose-specific opaque credential values."""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class InvalidOpaqueToken(ValueError):
    """Raised for a malformed or non-canonical opaque token."""


@dataclass(frozen=True, slots=True, repr=False)
class OpaqueToken:
    """A canonical unpadded base64url token backed by exactly 256 random bits."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if _TOKEN_PATTERN.fullmatch(self._value) is None:
            raise InvalidOpaqueToken("opaque token must be canonical unpadded base64url")
        try:
            raw = base64.urlsafe_b64decode(f"{self._value}=")
        except (ValueError, UnicodeEncodeError):
            raise InvalidOpaqueToken("opaque token must be canonical unpadded base64url") from None
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != 32 or canonical != self._value:
            raise InvalidOpaqueToken("opaque token must encode exactly 256 bits")

    def reveal(self) -> str:
        """Return the wire value at an explicit credential boundary."""
        return self._value

    def digest(self) -> bytes:
        """Return the SHA-256 digest used for persistent lookup."""
        raw = base64.urlsafe_b64decode(f"{self._value}=")
        return hashlib.sha256(raw).digest()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"


class RefreshToken(OpaqueToken):
    """A refresh credential that cannot be substituted for another purpose."""


class RegistrationToken(OpaqueToken):
    """An email-registration credential."""


class PasswordResetToken(OpaqueToken):
    """A password-reset credential."""


class CsrfToken(OpaqueToken):
    """A double-submit CSRF credential."""


def csrf_tokens_match(cookie_token: CsrfToken, header_token: CsrfToken) -> bool:
    """Compare CSRF wire values without data-dependent early exit."""
    return hmac.compare_digest(cookie_token.reveal(), header_token.reveal())


def generate_opaque_token[TokenT: OpaqueToken](
    token_type: type[TokenT],
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> TokenT:
    """Generate one purpose-specific credential from an independent 256-bit sample."""
    raw = random_bytes(32)
    if len(raw) != 32:
        raise ValueError("random source must return exactly 32 bytes")
    wire_value = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return token_type(wire_value)
