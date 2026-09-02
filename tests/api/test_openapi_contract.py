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

    assert live["description"] == "The service process is running."
    assert live["content"]["application/json"]["example"]["message"] == ("The service is running.")
    assert ready["description"] == "The service is ready to accept traffic."
    assert ready["content"]["application/json"]["example"]["message"] == ("The service is ready.")


@pytest.mark.api
def test_problem_details_uses_only_its_declared_media_type() -> None:
    response = app.openapi()["paths"]["/health/ready"]["get"]["responses"]["503"]

    assert set(response["content"]) == {"application/problem+json"}


@pytest.mark.api
def test_machine_readable_health_and_problem_fields_are_constrained() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["HealthResponse"]["properties"]["status"]["const"] == "ok"
    assert schemas["ProblemDetails"]["properties"]["type"]["format"] == "uri"
