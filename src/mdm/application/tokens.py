"""Application policy for issuing persistent opaque credentials."""

import secrets
from collections.abc import Awaitable, Callable

from mdm.domain.credentials import OpaqueToken, generate_opaque_token


class OpaqueTokenCollision(RuntimeError):
    """Credential issuance failed without exposing token material."""


async def issue_unique_opaque_token[TokenT: OpaqueToken](
    token_type: type[TokenT],
    *,
    digest_exists: Callable[[bytes], Awaitable[bool]],
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> TokenT:
    """Generate a token, allowing three regenerations after digest collisions."""
    for _ in range(4):
        token = generate_opaque_token(token_type, random_bytes=random_bytes)
        if not await digest_exists(token.digest()):
            return token
    raise OpaqueTokenCollision("opaque token issuance failed")
