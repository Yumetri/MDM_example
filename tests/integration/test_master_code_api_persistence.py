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


def _not_applicable_dimensions() -> dict[str, object]:
    return {
        slot: {"mode": "NOT_APPLICABLE"}
        for slot in (
            "company",
            "brand",
            "model",
            "category",
            "year",
            "memory",
            "network",
            "country",
        )
    }


@pytest.mark.integration
async def test_master_code_routes_cross_real_auth_repository_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = Settings().reveal_database_url()
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE master_code_logs, master_codes, "
                "dimension_company_logs, dimension_companies, "
                "dimension_brand_logs, dimension_brands, "
                "dimension_model_logs, dimension_models, "
                "dimension_category_logs, dimension_categories, "
                "dimension_year_logs, dimension_years, "
                "dimension_memory_logs, dimension_memories, "
                "dimension_network_logs, dimension_networks, "
                "dimension_country_logs, dimension_countries CASCADE"
            )
        )
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
    dimensions = _not_applicable_dimensions()

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            company = await client.post(
                "/api/v1/dimensions/companies",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"code": "COM", "value": "Company"},
            )
            denied = await client.post(
                "/api/v1/master-codes",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"dimensions": dimensions},
            )
            created = await client.post(
                "/api/v1/master-codes",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"dimensions": dimensions, "reason": "최초 등록"},
            )
            duplicate = await client.post(
                "/api/v1/master-codes",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"dimensions": dimensions},
            )
            listed = await client.get(
                "/api/v1/master-codes", headers={"Authorization": f"Bearer {user_token}"}
            )
            detail = await client.get(
                f"/api/v1/master-codes/{created.json()['id']}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            denied_update = await client.patch(
                f"/api/v1/master-codes/{created.json()['id']}",
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "If-Match": created.headers["etag"],
                },
                json={"dimensions": {"company": {"mode": "REFERENCE", "id": company.json()["id"]}}},
            )
            updated = await client.patch(
                f"/api/v1/master-codes/{created.json()['id']}",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "If-Match": created.headers["etag"],
                },
                json={
                    "dimensions": {"company": {"mode": "REFERENCE", "id": company.json()["id"]}},
                    "reason": "Company 참조 연결",
                },
            )
            stale = await client.patch(
                f"/api/v1/master-codes/{created.json()['id']}",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "If-Match": created.headers["etag"],
                },
                json={"dimensions": {"company": {"mode": "NOT_APPLICABLE"}}},
            )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["dimensions"] == dict.fromkeys(dimensions)
    assert created.headers["etag"].startswith('"mc-1-')
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "MASTER_CODE_CONFLICT"
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]
    assert detail.status_code == 200
    assert detail.json() == created.json()
    assert detail.headers["etag"] == created.headers["etag"]
    assert denied_update.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["code"] == "COM-NNN-NNN-NNN-NNN-NNN-NNN-NNN"
    assert updated.json()["version"] == 2
    assert updated.json()["dimensions"]["company"]["id"] == company.json()["id"]
    assert updated.headers["etag"] != created.headers["etag"]
    assert stale.status_code == 412
    assert stale.json()["code"] == "PRECONDITION_FAILED"

    engine = create_engine(database_url)
    async with engine.connect() as connection:
        operations = (
            await connection.scalars(
                text(
                    "SELECT operation FROM master_code_logs "
                    "WHERE master_code_id = :id ORDER BY master_code_version"
                ),
                {"id": UUID(created.json()["id"])},
            )
        ).all()
    await engine.dispose()
    assert operations == ["CREATE", "REFERENCE_UPDATE"]
