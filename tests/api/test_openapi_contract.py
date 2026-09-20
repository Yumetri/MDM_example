import base64
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from mdm.main import app


def public_operations(schema: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method.lower() in {"delete", "get", "head", "options", "patch", "post", "put"}:
                operations.append(operation)
    return operations


@pytest.mark.api
def test_openapi_operations_are_documented_for_people_and_agents() -> None:
    schema = app.openapi()
    operations = public_operations(schema)

    assert operations
    assert all(operation.get("operationId") for operation in operations)
    assert all(operation.get("summary") for operation in operations)
    assert all(operation.get("description") for operation in operations)
    assert all(operation.get("tags") for operation in operations)

    operation_ids = [operation["operationId"] for operation in operations]
    duplicates = [name for name, count in Counter(operation_ids).items() if count > 1]
    assert duplicates == []


@pytest.mark.api
def test_all_service_routes_use_the_global_v1_prefix() -> None:
    paths = app.openapi()["paths"]

    assert paths
    assert all(path.startswith("/api/v1/") for path in paths)


@pytest.mark.api
def test_openapi_success_responses_have_schemas() -> None:
    for operation in public_operations(app.openapi()):
        success_responses = [
            (status, response)
            for status, response in operation["responses"].items()
            if status.startswith("2")
        ]
        assert success_responses
        for status, response in success_responses:
            if status == "204":
                assert "content" not in response
            else:
                assert response.get("content", {}).get("application/json", {}).get("schema")


@pytest.mark.api
def test_public_schema_fields_have_descriptions() -> None:
    schemas = app.openapi()["components"]["schemas"]
    missing = [
        f"{schema_name}.{field_name}"
        for schema_name, schema in schemas.items()
        for field_name, field in schema.get("properties", {}).items()
        if not field.get("description")
    ]
    assert missing == []


@pytest.mark.api
def test_readiness_documents_problem_details_example() -> None:
    operation = app.openapi()["paths"]["/api/v1/health/ready"]["get"]
    response = operation["responses"]["503"]

    assert response["content"]["application/problem+json"]["schema"]
    assert response["content"]["application/problem+json"]["example"]["code"] == (
        "SERVICE_UNAVAILABLE"
    )


@pytest.mark.api
def test_health_responses_have_endpoint_specific_descriptions_and_examples() -> None:
    paths = app.openapi()["paths"]
    live = paths["/api/v1/health/live"]["get"]["responses"]["200"]
    ready = paths["/api/v1/health/ready"]["get"]["responses"]["200"]

    assert live["description"] == "서비스 프로세스가 실행 중입니다."
    assert live["content"]["application/json"]["example"]["message"] == ("서비스가 실행 중입니다.")
    assert ready["description"] == "서비스가 요청을 처리할 준비가 되었습니다."
    assert ready["content"]["application/json"]["example"]["message"] == (
        "서비스가 요청을 처리할 준비가 되었습니다."
    )


@pytest.mark.api
def test_openapi_user_facing_documentation_is_korean() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    assert schema["info"]["summary"] == "Dimension 기반 마스터 데이터 관리"
    assert schema["info"]["description"] == (
        "Dimension을 참조해 고유 코드를 생성하는 마스터 데이터를 관리하는 서비스 API입니다."
    )
    assert schema["tags"] == [
        {
            "name": "AdminUsers",
            "description": (
                "관리자가 권한 범위 안에서 사용자를 조회하고 역할·활성 상태를 변경합니다."
            ),
        },
        {
            "name": "Authentication",
            "description": "이메일 인증 가입·로그인·세션 갱신·로그아웃과 현재 프로필을 제공합니다.",
        },
        {
            "name": "Health",
            "description": "서비스의 실행 상태와 요청 처리 준비 상태를 각각 확인합니다.",
        },
        {
            "name": "Company Dimensions",
            "description": "Company Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Model Dimensions",
            "description": "Model Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Brand Dimensions",
            "description": "Brand Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Country Dimensions",
            "description": "Country Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Category Dimensions",
            "description": "Category Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Year Dimensions",
            "description": "Year Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Network Dimensions",
            "description": "Network Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "Memory Dimensions",
            "description": "Memory Dimension을 생성·조회·수정·삭제·복원합니다.",
        },
        {
            "name": "MasterCodes",
            "description": "Dimension 참조를 합성한 MasterCode를 생성·수정하고 조회합니다.",
        },
        {
            "name": "AuditLogs",
            "description": "관리자가 Dimension·MasterCode의 변경 이력을 조회합니다.",
        },
    ]

    live = paths["/api/v1/health/live"]["get"]
    ready = paths["/api/v1/health/ready"]["get"]
    assert live["summary"] == "서비스 프로세스 실행 여부 확인"
    assert live["description"] == (
        "서비스 프로세스가 실행 중이며 생존 확인 요청에 응답하면 성공을 반환합니다. "
        "이 확인은 데이터베이스 상태와 무관합니다."
    )
    assert ready["summary"] == "서비스 요청 처리 가능 여부 확인"
    assert ready["description"] == (
        "요청 처리에 필요한 데이터베이스 연결 상태를 확인합니다. 데이터베이스를 사용할 수 없어 "
        "서비스를 트래픽에서 일시적으로 제외해야 하면 503 응답을 반환합니다."
    )
    assert ready["responses"]["503"]["description"] == "데이터베이스를 사용할 수 없습니다."
    assert ready["responses"]["503"]["content"]["application/problem+json"]["example"] == {
        "type": "/problems/service-unavailable",
        "title": "서비스를 사용할 수 없음",
        "status": 503,
        "detail": "데이터베이스 연결을 확인할 수 없어 현재 요청을 처리할 수 없습니다.",
        "code": "SERVICE_UNAVAILABLE",
        "instance": "/api/v1/health/ready",
    }

    assert schemas["HealthResponse"]["description"] == "상태 확인 엔드포인트가 반환하는 결과입니다."
    assert schemas["HealthResponse"]["example"] == {
        "status": "ok",
        "message": "상태 확인이 성공했습니다.",
    }
    assert {
        name: field["description"]
        for name, field in schemas["HealthResponse"]["properties"].items()
    } == {
        "status": "상태 확인 결과를 나타내는 짧고 기계 판독 가능한 값입니다.",
        "message": "현재 서비스 상태를 설명하는 사용자용 메시지입니다.",
    }
    assert schemas["FieldViolation"]["description"] == (
        "입력값 또는 기존 참조의 검증에 실패한 필드 하나에 대한 정보입니다."
    )
    assert {
        name: field["description"]
        for name, field in schemas["FieldViolation"]["properties"].items()
    } == {
        "field": "유효하지 않은 입력값의 위치 또는 기존 참조의 식별자입니다.",
        "message": "입력값 또는 기존 참조의 검증 실패 원인을 설명하는 사용자용 메시지입니다.",
    }
    assert schemas["ProblemDetails"]["description"] == (
        "안정적인 서비스 오류 코드를 추가한 RFC 9457 Problem Details입니다."
    )
    assert {
        name: field["description"]
        for name, field in schemas["ProblemDetails"]["properties"].items()
    } == {
        "type": (
            "문제 유형을 식별하는 안정적인 상대 URI입니다. 현재 API origin을 기준으로 "
            "해석하며, code는 서비스 전용 보조 식별자입니다."
        ),
        "title": "문제를 짧게 요약한 사용자용 문구입니다.",
        "status": "해당 문제 발생에 대해 반환한 HTTP 상태 코드입니다.",
        "detail": "해당 문제 발생의 구체적인 원인을 설명하는 사용자용 문구입니다.",
        "code": "안정적이고 기계 판독 가능한 서비스 오류 코드입니다.",
        "instance": "이 문제 발생을 식별하는 URI 참조입니다.",
        "violations": "입력값 검증 또는 충돌과 관련된 필드별 문제 목록입니다.",
    }


@pytest.mark.api
def test_company_routes_are_registered_in_the_production_schema() -> None:
    paths = app.openapi()["paths"]

    assert set(paths["/api/v1/dimensions/companies"]) >= {"get", "post"}
    assert set(paths["/api/v1/dimensions/companies/{company_id}"]) >= {"get", "patch"}


@pytest.mark.api
def test_string_dimension_routes_are_registered_in_the_production_schema() -> None:
    paths = app.openapi()["paths"]

    for collection in ("models", "brands", "countries", "categories"):
        assert set(paths[f"/api/v1/dimensions/{collection}"]) >= {"get", "post"}
        assert set(paths[f"/api/v1/dimensions/{collection}/{{dimension_id}}"]) >= {"get", "patch"}


@pytest.mark.api
def test_dimension_patch_schemas_require_code_or_value_without_accepting_null() -> None:
    schemas = app.openapi()["components"]["schemas"]

    for schema_name in (
        "CompanyValueUpdateRequest",
        "ModelValueUpdateRequest",
        "BrandValueUpdateRequest",
        "CountryValueUpdateRequest",
        "CategoryValueUpdateRequest",
        "YearValueUpdateRequest",
        "NetworkValueUpdateRequest",
        "MemoryValueUpdateRequest",
    ):
        request_schema = schemas[schema_name]

        assert request_schema["anyOf"] == [
            {"required": ["code"]},
            {"required": ["value"]},
        ]
        for field_name in ("code", "value"):
            field_schema = request_schema["properties"][field_name]
            assert field_schema.get("type") != "null"
            assert all(option.get("type") != "null" for option in field_schema.get("anyOf", []))


@pytest.mark.api
def test_problem_details_uses_only_its_declared_media_type() -> None:
    response = app.openapi()["paths"]["/api/v1/health/ready"]["get"]["responses"]["503"]

    assert set(response["content"]) == {"application/problem+json"}


@pytest.mark.api
def test_machine_readable_health_and_problem_fields_are_constrained() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["HealthResponse"]["properties"]["status"]["const"] == "ok"
    assert schemas["ProblemDetails"]["properties"]["type"]["format"] == "uri-reference"
    assert schemas["ProblemDetails"]["properties"]["instance"]["format"] == "uri-reference"
    assert schemas["ProblemDetails"]["properties"]["instance"]["type"] == "string"
    assert "anyOf" not in schemas["ProblemDetails"]["properties"]["instance"]
    assert "instance" not in schemas["ProblemDetails"]["required"]


@pytest.mark.api
def test_company_examples_match_the_active_response_and_cursor_contract() -> None:
    schema = app.openapi()
    company_schema = schema["components"]["schemas"]["CompanyResponse"]
    list_schema = schema["components"]["schemas"]["CompanyListResponse"]
    parameters = schema["paths"]["/api/v1/dimensions/companies"]["get"]["parameters"]
    cursor_parameter = next(item for item in parameters if item["name"] == "cursor")

    assert company_schema["properties"]["deleted_at"]["type"] == "null"
    assert company_schema["example"]["deleted_at"] is None
    assert list_schema["example"]["items"][0]["deleted_at"] is None
    assert company_schema["example"]["code"] == "SAM01"
    assert company_schema["example"]["id"] == "01a083c3-88e8-7123-8000-000000000001"
    assert company_schema["example"]["created_at"].endswith("Z")
    assert list_schema["example"]["next_cursor"] == cursor_parameter["schema"]["examples"][0]
    assert (
        list_schema["properties"]["next_cursor"]["examples"][0]
        == (cursor_parameter["schema"]["examples"][0])
    )


@pytest.mark.api
def test_dimension_uuid7_timestamps_and_cursors_match_their_examples() -> None:
    schema = app.openapi()
    for name, collection in (
        ("Company", "companies"),
        ("Model", "models"),
        ("Brand", "brands"),
        ("Country", "countries"),
        ("Category", "categories"),
        ("Year", "years"),
        ("Network", "networks"),
        ("Memory", "memories"),
    ):
        example = schema["components"]["schemas"][f"{name}Response"]["example"]
        uuid_timestamp = datetime.fromtimestamp(
            (UUID(example["id"]).int >> 80) / 1000,
            tz=UTC,
        ).replace(microsecond=0)
        assert uuid_timestamp == datetime.fromisoformat(
            example["created_at"].replace("Z", "+00:00")
        )

        parameters = schema["paths"][f"/api/v1/dimensions/{collection}"]["get"]["parameters"]
        encoded = next(item for item in parameters if item["name"] == "cursor")["schema"][
            "examples"
        ][0]
        cursor = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        assert cursor == {"v": 1, "t": example["created_at"], "i": example["id"]}


@pytest.mark.api
@pytest.mark.parametrize(
    ("name", "collection"),
    [
        ("Company", "companies"),
        ("Brand", "brands"),
        ("Model", "models"),
        ("Category", "categories"),
        ("Country", "countries"),
        ("Year", "years"),
        ("Network", "networks"),
        ("Memory", "memories"),
    ],
)
def test_dimension_lifecycle_openapi_preserves_required_body_and_typed_states(name, collection):
    schema = app.openapi()
    models = schema["components"]["schemas"]
    base = f"/api/v1/dimensions/{collection}/{{dimension_id}}"
    tombstone = models[f"{name}TombstoneResponse"]
    assert tombstone["properties"]["deleted_at"]["type"] == "string"
    assert tombstone["properties"]["deleted_at"]["format"] == "date-time"
    assert tombstone["example"]["deleted_at"] is not None
    assert models[f"{name}Response"]["properties"]["deleted_at"]["type"] == "null"
    for action in ("delete", "restore", "tombstone"):
        method = "get" if action == "tombstone" else "post"
        operation = schema["paths"][f"{base}/{action}"][method]
        assert operation["tags"] == [f"{name} Dimensions"]
        for status, error_response in operation["responses"].items():
            if int(status) >= 400:
                assert error_response["content"]["application/problem+json"]["schema"] == {
                    "$ref": "#/components/schemas/ProblemDetails"
                }
        response = operation["responses"]["200"]
        model = f"{name}Response" if action == "restore" else f"{name}TombstoneResponse"
        assert (
            response["content"]["application/json"]["schema"]["$ref"]
            == f"#/components/schemas/{model}"
        )
        if action == "tombstone":
            assert "requestBody" not in operation
        else:
            assert operation["requestBody"]["required"] is True
            request_model = models["DimensionLifecycleRequest"]
            assert set(request_model["properties"]) == {"reason"}
            assert request_model["additionalProperties"] is False
            assert {"400", "412", "428"} <= operation["responses"].keys()
            if_match = next(p for p in operation["parameters"] if p["name"] == "If-Match")
            assert if_match["required"] is True
        example = response["content"]["application/json"].get("example", tombstone["example"])
        assert response["headers"]["ETag"]["example"] == f'"{example["version"]}"'
    restored_example = schema["paths"][f"{base}/restore"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    assert restored_example["deleted_at"] is None
    value_schema = tombstone["properties"]["value"]
    if name == "Year":
        assert (value_schema["minimum"], value_schema["maximum"]) == (2000, 2999)
    elif name == "Network":
        assert (value_schema["minimum"], value_schema["maximum"]) == (1, 5)
    elif name == "Memory":
        assert value_schema["$ref"] == "#/components/schemas/MemoryValueResponse"


@pytest.mark.api
def test_master_code_lifecycle_documents_required_body_state_and_aggregate_etag():
    schema = app.openapi()
    paths = schema["paths"]
    for action, method, operation_id, model in (
        ("delete", "post", "delete_master_code", "MasterCodeTombstoneResponse"),
        ("tombstone", "get", "get_master_code_tombstone", "MasterCodeTombstoneResponse"),
        ("restore", "post", "restore_master_code", "MasterCodeResponse"),
    ):
        operation = paths[f"/api/v1/master-codes/{{master_code_id}}/{action}"][method]
        assert operation["operationId"] == operation_id
        response = operation["responses"]["200"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(model)
        assert "ETag" in response["headers"]
        example = response["content"]["application/json"]["example"]
        assert "deleted_at" in example
        assert (example["deleted_at"] is None) == (action == "restore")
        assert len(example["dimensions"]) == 8
        assert {"401", "403", "404", "422", "503"} <= operation["responses"].keys()
        if method == "post":
            assert operation["requestBody"]["required"] is True
            assert {"400", "412", "428"} <= operation["responses"].keys()
            assert any(p["name"] == "If-Match" and p["required"] for p in operation["parameters"])
        if action != "delete":
            assert "409" in operation["responses"]
    request = schema["components"]["schemas"]["MasterCodeLifecycleRequest"]
    assert request["additionalProperties"] is False
    assert set(request["properties"]) == {"reason"}
