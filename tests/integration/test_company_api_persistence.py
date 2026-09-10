import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

from mdm.domain.auth import UserRole
from mdm.infrastructure.database import create_engine
from mdm.infrastructure.jwt import build_access_jwt_codec
from mdm.infrastructure.settings import Settings
from mdm.main import create_app

ADMIN_ID = UUID("01890f7c-8abc-7def-8abc-111111111111")
USER_ID = UUID("01890f7c-8abc-7def-8abc-222222222222")


def _write_key_files(directory: Path) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = directory / "jwt-private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "integration-key", "alg": "RS256", "use": "sig"})
    jwks_path = directory / "jwt-public-keys.json"
    jwks_path.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    return private_path, jwks_path


@pytest.mark.integration
async def test_real_jwt_api_repository_trigger_and_reads_form_one_vertical_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = Settings().reveal_database_url()
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE dimension_company_logs, dimension_companies"))
    await engine.dispose()

    private_path, jwks_path = _write_key_files(tmp_path)
    monkeypatch.setenv("MDM_AUTH_JWT_ACTIVE_KID", "integration-key")
    monkeypatch.setenv("MDM_AUTH_JWT_PRIVATE_KEY_PATH", str(private_path))
    monkeypatch.setenv("MDM_AUTH_JWT_JWKS_PATH", str(jwks_path))
    settings = Settings()
    codec = build_access_jwt_codec(settings)
    issued_at = int(datetime.now(UTC).timestamp())
    admin_token = codec.issue(ADMIN_ID, UserRole.ADMIN, issued_at=issued_at).reveal()
    user_token = codec.issue(USER_ID, UserRole.USER, issued_at=issued_at).reveal()
    application = create_app()

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/dimensions/companies",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"code": "sam", "value": " Samsung  Electronics ", "reason": "등록"},
            )
            listed = await client.get(
                "/dimensions/companies",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            detail = await client.get(
                f"/dimensions/companies/{created.json()['id']}",
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json()["value"] == "SAMSUNG_ELECTRONICS"
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]
    assert listed.json()["next_cursor"] is None
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'
    assert detail.json() == created.json()
