"""OpenAPI normalization for public response contracts."""

from typing import Any

from fastapi import FastAPI


def configure_openapi(application: FastAPI) -> None:
    """Ensure Problem Details responses advertise only their actual media type."""
    schema: dict[str, Any] = application.openapi()
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            for response in operation.get("responses", {}).values():
                content = response.get("content", {})
                if "application/problem+json" in content:
                    content.pop("application/json", None)
    schemas = schema.get("components", {}).get("schemas", {})
    for name in (
        "Company",
        "Model",
        "Brand",
        "Country",
        "Category",
        "Year",
        "Network",
        "Memory",
    ):
        response_schema = schemas.get(f"{name}Response")
        if response_schema is not None:
            response_schema["example"]["deleted_at"] = None
        list_schema = schemas.get(f"{name}ListResponse")
        if list_schema is not None:
            for item in list_schema["example"]["items"]:
                item["deleted_at"] = None
    master_code_response = schemas.get("MasterCodeResponse")
    if master_code_response is not None:
        _restore_master_code_nulls(master_code_response["example"])
    master_code_list = schemas.get("MasterCodeListResponse")
    if master_code_list is not None:
        for item in master_code_list["example"]["items"]:
            _restore_master_code_nulls(item)
    paths = schema.get("paths", {})
    master_code_collection = paths.get("/api/v1/master-codes", {})
    for method, status in (("post", "201"), ("get", "200")):
        operation = master_code_collection.get(method)
        if operation is None:
            continue
        example = operation["responses"][status]["content"]["application/json"].get("example")
        if example is None:
            continue
        if method == "post":
            _restore_master_code_nulls(example)
        else:
            for item in example["items"]:
                _restore_master_code_nulls(item)
    detail = paths.get("/api/v1/master-codes/{master_code_id}", {})
    for method in ("get", "patch"):
        operation = detail.get(method)
        if operation is None:
            continue
        example = operation["responses"]["200"]["content"]["application/json"].get("example")
        if example is not None:
            _restore_master_code_nulls(example)
    application.openapi_schema = schema


def _restore_master_code_nulls(example: dict[str, Any]) -> None:
    """Keep required JSON nulls that FastAPI drops while encoding OpenAPI examples."""
    example["deleted_at"] = None
    dimensions = example["dimensions"]
    for slot in (
        "company",
        "brand",
        "model",
        "category",
        "year",
        "memory",
        "network",
        "country",
    ):
        dimensions.setdefault(slot, None)
