import base64
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

CASES = (
    ("years", "YR2026", 2026),
    ("networks", "NET5", 5),
)


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
async def test_numeric_dimension_routes_cross_real_auth_repository_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = Settings().reveal_database_url()
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE dimension_year_logs, dimension_years, "
                "dimension_network_logs, dimension_networks"
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

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for collection, code, value in CASES:
                denied = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {user_token}"},
                    json={"code": code, "value": value},
                )
                created = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"code": code, "value": value, "reason": "등록"},
                )
                listed = await client.get(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                detail = await client.get(
                    f"/dimensions/{collection}/{created.json()['id']}",
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                duplicate = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"code": code, "value": value - 1},
                )
                spoofed = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={
                        "code": f"{code}X",
                        "value": value,
                        "actor_kind": "SYSTEM",
                        "actor_role": "SUPER_ADMIN",
                    },
                )
                missing = await client.get(
                    f"/dimensions/{collection}/01890f7c-8abc-7def-8abc-000000000000",
                    headers={"Authorization": f"Bearer {user_token}"},
                )

                assert denied.status_code == 403
                assert denied.json()["code"] == "AUTHORIZATION_DENIED"
                assert created.status_code == 201
                assert created.headers["etag"] == '"1"'
                assert created.json()["value"] == value
                assert listed.status_code == 200
                assert listed.json()["items"] == [created.json()]
                assert listed.json()["next_cursor"] is None
                assert detail.status_code == 200
                assert detail.headers["etag"] == '"1"'
                assert detail.json() == created.json()
                assert duplicate.status_code == 409
                assert duplicate.json()["code"] == "DIMENSION_CODE_CONFLICT"
                assert spoofed.status_code == 422
                assert spoofed.json()["code"] == "VALIDATION_ERROR"
                assert missing.status_code == 404
                assert missing.json()["code"] == "DIMENSION_NOT_FOUND"

            for collection, invalid_values in (
                ("years", ("2026", 2026.0, True, 1999, 3000)),
                ("networks", ("5", 5.0, True, 0, 6)),
            ):
                for index, invalid_value in enumerate(invalid_values):
                    response = await client.post(
                        f"/dimensions/{collection}",
                        headers={"Authorization": f"Bearer {admin_token}"},
                        json={"code": f"BAD{index}", "value": invalid_value},
                    )
                    assert response.status_code == 422
                    assert response.json()["code"] == "VALIDATION_ERROR"
                    assert response.json()["violations"][0]["field"] == "body.value"


@pytest.mark.api
def test_numeric_dimension_openapi_has_stable_operations_and_strict_integer_schemas() -> None:
    schema = create_app().openapi()

    for singular, display_name, collection, minimum, maximum in (
        ("year", "Year", "years", 2000, 2999),
        ("network", "Network", "networks", 1, 5),
    ):
        collection_path = schema["paths"][f"/dimensions/{collection}"]
        detail_path = schema["paths"][f"/dimensions/{collection}/{{dimension_id}}"]
        assert collection_path["post"]["operationId"] == f"create_{singular}_dimension"
        assert collection_path["get"]["operationId"] == f"list_{singular}_dimensions"
        assert detail_path["get"]["operationId"] == f"get_{singular}_dimension"
        request_schema = schema["components"]["schemas"][f"{display_name}CreateRequest"]
        value_schema = request_schema["properties"]["value"]
        assert value_schema["type"] == "integer"
        assert value_schema["minimum"] == minimum
        assert value_schema["maximum"] == maximum
        response_schema = schema["components"]["schemas"][f"{display_name}Response"]
        assert response_schema["example"]["deleted_at"] is None
        list_example = schema["components"]["schemas"][f"{display_name}ListResponse"]["example"]
        encoded_cursor = list_example["next_cursor"]
        cursor_payload = json.loads(
            base64.urlsafe_b64decode(encoded_cursor + "=" * (-len(encoded_cursor) % 4))
        )
        assert cursor_payload["i"] == list_example["items"][0]["id"]
        assert cursor_payload["t"] == list_example["items"][0]["created_at"]
        for operation, statuses in (
            (collection_path["post"], ("409", "422", "503")),
            (collection_path["get"], ("422", "503")),
            (detail_path["get"], ("404", "422", "503")),
        ):
            for status_code in statuses:
                assert operation["responses"][status_code]["content"]["application/problem+json"][
                    "schema"
                ] == {"$ref": "#/components/schemas/ProblemDetails"}
