"""Local-only RSA key file generation for development environments."""

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


def generate_local_jwt_keys(*, kid: str, private_key_path: Path, jwks_path: Path) -> None:
    """Create a 0600 private PEM and matching public-only JWKS without overwriting."""
    if not kid or not kid.isascii():
        raise ValueError("kid must be a non-empty ASCII identifier")
    if private_key_path == jwks_path:
        raise ValueError("private key and JWKS paths must be different")
    if private_key_path.exists():
        raise FileExistsError(private_key_path)
    if jwks_path.exists():
        raise FileExistsError(jwks_path)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    jwks_bytes = (
        json.dumps({"keys": [public_jwk]}, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode()

    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    jwks_path.parent.mkdir(parents=True, exist_ok=True)
    private_created = False
    try:
        _write_exclusive(private_key_path, private_bytes, mode=0o600)
        private_created = True
        _write_exclusive(jwks_path, jwks_bytes, mode=0o644)
    except BaseException:
        if private_created:
            private_key_path.unlink(missing_ok=True)
        raise


def _write_exclusive(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
