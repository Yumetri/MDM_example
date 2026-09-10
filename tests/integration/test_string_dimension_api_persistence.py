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
    ("models", "MOD1", " Galaxy__S24 Ultra ", "GALAXY_S24_ULTRA"),
    ("brands", "BRA1", " Samsung ", "SAMSUNG"),
    ("countries", "KOR", " South  Korea ", "SOUTH_KOREA"),
    ("categories", "PHN", " Smart Phone ", "SMART_PHONE"),
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
async def test_all_string_dimension_routes_cross_real_auth_repository_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = Settings().reveal_database_url()
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE dimension_model_logs, dimension_models, "
                "dimension_brand_logs, dimension_brands, "
                "dimension_country_logs, dimension_countries, "
                "dimension_category_logs, dimension_categories"
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
            for collection, code, raw_value, normalized_value in CASES:
                denied = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {user_token}"},
                    json={"code": code, "value": raw_value},
                )
                created = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"code": code, "value": raw_value, "reason": "등록"},
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
                    json={"code": code, "value": f"{raw_value} SECOND"},
                )
                spoofed = await client.post(
                    f"/dimensions/{collection}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={
                        "code": f"{code}X",
                        "value": f"{raw_value} SECOND",
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
                assert created.json()["value"] == normalized_value
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


@pytest.mark.api
def test_string_dimension_openapi_has_stable_operations_and_type_specific_schemas() -> None:
    schema = create_app().openapi()

    for singular, display_name, collection, object_phrase in (
        ("model", "Model", "models", "Model을"),
        ("brand", "Brand", "brands", "Brand를"),
        ("country", "Country", "countries", "Country를"),
        ("category", "Category", "categories", "Category를"),
    ):
        collection_path = schema["paths"][f"/dimensions/{collection}"]
        detail_path = schema["paths"][f"/dimensions/{collection}/{{dimension_id}}"]
        assert collection_path["post"]["operationId"] == f"create_{singular}_dimension"
        assert collection_path["get"]["operationId"] == f"list_{singular}_dimensions"
        assert detail_path["get"]["operationId"] == f"get_{singular}_dimension"
        assert f"활성 {object_phrase}" in collection_path["get"]["description"]
        assert "cursor는 만료되지 않습니다" in collection_path["get"]["description"]
        assert "이미 삭제된 항목은 결과에서 제외됩니다" in collection_path["get"]["description"]
        assert f"활성 {object_phrase}" in detail_path["get"]["description"]
        assert (
            schema["components"]["schemas"][f"{display_name}Response"]["example"]["deleted_at"]
            is None
        )
        list_schema = schema["components"]["schemas"][f"{display_name}ListResponse"]
        assert list_schema["description"] == (
            f"최신 생성 순서로 조회한 {display_name} cursor 페이지입니다."
        )
        list_example = list_schema["example"]
        assert list_example["items"][0]["deleted_at"] is None
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
                problem = operation["responses"][status_code]
                assert problem["description"] not in {
                    "Conflict",
                    "Not Found",
                    "Unprocessable Content",
                    "Service Unavailable",
                }
                assert problem["content"]["application/problem+json"]["schema"] == {
                    "$ref": "#/components/schemas/ProblemDetails"
                }

    component_names = set(schema["components"]["schemas"])
    assert {
        "ModelCreateRequest",
        "ModelResponse",
        "ModelListResponse",
        "BrandCreateRequest",
        "BrandResponse",
        "BrandListResponse",
        "CountryCreateRequest",
        "CountryResponse",
        "CountryListResponse",
        "CategoryCreateRequest",
        "CategoryResponse",
        "CategoryListResponse",
    } <= component_names

    request_examples = {
        "ModelCreateRequest": ("s24u", " Galaxy S24 Ultra "),
        "BrandCreateRequest": ("sam", " Samsung "),
        "CountryCreateRequest": ("kor", " South Korea "),
        "CategoryCreateRequest": ("phn", " Smart Phone "),
    }
    for schema_name, (code, value) in request_examples.items():
        properties = schema["components"]["schemas"][schema_name]["properties"]
        assert properties["code"]["examples"] == [code]
        assert properties["value"]["examples"] == [value]
