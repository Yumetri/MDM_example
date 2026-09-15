"""Keyed IP digests and a default resolver that makes no unverified trust claims."""

import base64
import hashlib
import hmac

from pydantic import SecretStr

from mdm.application.request_protection import ClientIp
from mdm.domain.credentials import InvalidOpaqueToken, OpaqueToken


def decode_ip_hmac_secret(secret: SecretStr) -> bytes:
    """Require an independently provisioned 32-byte canonical base64url secret."""
    try:
        value = OpaqueToken(secret.get_secret_value())
    except InvalidOpaqueToken:
        raise ValueError(
            "IP-HMAC secret must encode exactly 32 bytes in canonical base64url"
        ) from None
    return base64.urlsafe_b64decode(value.reveal() + "=")


class HmacClientIpKey:
    def __init__(self, secret: SecretStr) -> None:
        self._key = decode_ip_hmac_secret(secret)

    def __call__(self, address: ClientIp) -> str:
        digest = hmac.new(self._key, str(address).encode("ascii"), hashlib.sha256).hexdigest()
        return f"mdm:rate-limit:auth:{digest}"


class UnresolvedClientIpResolver:
    """Default until #34 verifies a concrete deployment's client address semantics."""

    def resolve(
        self, *, peer_host: str | None, headers: tuple[tuple[bytes, bytes], ...]
    ) -> ClientIp | None:
        del peer_host, headers
        return None
