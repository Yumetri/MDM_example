from collections import Counter
from typing import Any

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
def test_openapi_success_responses_have_schemas() -> None:
    for operation in public_operations(app.openapi()):
        success_responses = [
            response
            for status, response in operation["responses"].items()
            if status.startswith("2")
        ]
        assert success_responses
        assert all(
            response.get("content", {}).get("application/json", {}).get("schema")
            for response in success_responses
        )


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
    operation = app.openapi()["paths"]["/health/ready"]["get"]
    response = operation["responses"]["503"]

    assert response["content"]["application/problem+json"]["schema"]
    assert response["content"]["application/problem+json"]["example"]["code"] == (
        "SERVICE_UNAVAILABLE"
    )


@pytest.mark.api
def test_health_responses_have_endpoint_specific_descriptions_and_examples() -> None:
    paths = app.openapi()["paths"]
    live = paths["/health/live"]["get"]["responses"]["200"]
    ready = paths["/health/ready"]["get"]["responses"]["200"]

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
            "name": "Health",
            "description": "서비스의 실행 상태와 요청 처리 준비 상태를 각각 확인합니다.",
        },
        {
            "name": "Company Dimensions",
            "description": "Company Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Model Dimensions",
            "description": "Model Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Brand Dimensions",
            "description": "Brand Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Country Dimensions",
            "description": "Country Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Category Dimensions",
            "description": "Category Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Year Dimensions",
            "description": "Year Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
        {
            "name": "Network Dimensions",
            "description": "Network Dimension을 생성하고 활성 데이터를 조회합니다.",
        },
    ]

    live = paths["/health/live"]["get"]
    ready = paths["/health/ready"]["get"]
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
        "instance": "/health/ready",
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
        "입력값 검증에 실패한 필드 하나에 대한 정보입니다."
    )
    assert {
        name: field["description"]
        for name, field in schemas["FieldViolation"]["properties"].items()
    } == {
        "field": "유효하지 않은 입력값의 위치입니다.",
        "message": "입력값 검증 실패 원인을 설명하는 사용자용 메시지입니다.",
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
        "violations": "입력값 검증 실패 시 유효하지 않은 필드 목록입니다.",
    }


@pytest.mark.api
def test_company_routes_are_registered_in_the_production_schema() -> None:
    paths = app.openapi()["paths"]

    assert set(paths["/dimensions/companies"]) >= {"get", "post"}
    assert "get" in paths["/dimensions/companies/{company_id}"]


@pytest.mark.api
def test_string_dimension_routes_are_registered_in_the_production_schema() -> None:
    paths = app.openapi()["paths"]

    for collection in ("models", "brands", "countries", "categories"):
        assert set(paths[f"/dimensions/{collection}"]) >= {"get", "post"}
        assert "get" in paths[f"/dimensions/{collection}/{{dimension_id}}"]


@pytest.mark.api
def test_problem_details_uses_only_its_declared_media_type() -> None:
    response = app.openapi()["paths"]["/health/ready"]["get"]["responses"]["503"]

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
    parameters = schema["paths"]["/dimensions/companies"]["get"]["parameters"]
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
