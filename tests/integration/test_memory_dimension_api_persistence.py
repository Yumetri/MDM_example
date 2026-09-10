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
async def test_memory_routes_cross_real_auth_repository_generation_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = Settings().reveal_database_url()
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE dimension_memory_logs, dimension_memories"))
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
            denied = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"code": "MEM1TB", "value": {"amount": 1, "unit": "TB"}},
            )
            created = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "code": "mem1tb",
                    "value": {"amount": 1, "unit": " tb "},
                    "reason": "등록",
                },
            )
            listed = await client.get(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            detail = await client.get(
                f"/dimensions/memories/{created.json()['id']}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            equivalent = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"code": "MEM1000GB", "value": {"amount": 1000, "unit": "GB"}},
            )
            supplied_capacity = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "code": "FORGED",
                    "value": {"amount": 2, "unit": "TB", "capacity_mb": 1},
                },
            )
            invalid_unit = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"code": "INVALID", "value": {"amount": 2, "unit": "GiB"}},
            )
            spoofed = await client.post(
                "/dimensions/memories",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "code": "SPOOFED",
                    "value": {"amount": 2, "unit": "TB"},
                    "actor_kind": "SYSTEM",
                    "actor_role": "SUPER_ADMIN",
                },
            )

    assert denied.status_code == 403
    assert denied.json()["code"] == "AUTHORIZATION_DENIED"
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json()["code"] == "MEM1TB"
    assert created.json()["value"] == {
        "amount": 1,
        "unit": "TB",
        "capacity_mb": 1_000_000,
    }
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]
    assert detail.status_code == 200
    assert detail.json() == created.json()
    assert equivalent.status_code == 409
    assert equivalent.json()["code"] == "DIMENSION_VALUE_CONFLICT"
    assert equivalent.json()["violations"][0]["field"] == "body.value"
    assert supplied_capacity.status_code == 422
    assert invalid_unit.status_code == 422
    assert invalid_unit.json()["violations"][0]["field"] == "body.value.unit"
    assert spoofed.status_code == 422
