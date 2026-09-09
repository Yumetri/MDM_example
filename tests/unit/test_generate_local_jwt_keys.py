import json
import stat
from pathlib import Path

import pytest

from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.jwt_key_generation import generate_local_jwt_keys
from mdm.infrastructure.settings import Settings


@pytest.mark.unit
def test_local_jwt_key_generator_writes_private_pem_and_public_only_jwks(
    tmp_path: Path,
) -> None:
    private_key_path = tmp_path / "jwt-private.pem"
    jwks_path = tmp_path / "jwt-public-keys.json"

    generate_local_jwt_keys(
        kid="local-dev",
        private_key_path=private_key_path,
        jwks_path=jwks_path,
    )

    jwks = json.loads(jwks_path.read_text(encoding="utf-8"))
    jwk = jwks["keys"][0]
    assert private_key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert stat.S_IMODE(private_key_path.stat().st_mode) == 0o600
    assert jwk["kid"] == "local-dev"
    assert jwk["alg"] == "RS256"
    assert jwk["use"] == "sig"
    assert not {"d", "p", "q", "dp", "dq", "qi", "oth"}.intersection(jwk)

    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
        auth_jwt_active_kid="local-dev",
        auth_jwt_private_key_path=private_key_path,
        auth_jwt_jwks_path=jwks_path,
        _env_file=None,
    )
    build_access_jwt_codec(settings)


@pytest.mark.unit
def test_local_jwt_key_generator_never_overwrites_existing_key_material(
    tmp_path: Path,
) -> None:
    private_key_path = tmp_path / "jwt-private.pem"
    jwks_path = tmp_path / "jwt-public-keys.json"
    private_key_path.write_text("existing-private", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate_local_jwt_keys(
            kid="local-dev",
            private_key_path=private_key_path,
            jwks_path=jwks_path,
        )

    assert private_key_path.read_text(encoding="utf-8") == "existing-private"
    assert not jwks_path.exists()
