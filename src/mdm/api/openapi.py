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
    company_schema = schema.get("components", {}).get("schemas", {}).get("CompanyResponse")
    if company_schema is not None:
        company_schema["example"]["deleted_at"] = None
    company_list_schema = schema.get("components", {}).get("schemas", {}).get("CompanyListResponse")
    if company_list_schema is not None:
        for item in company_list_schema["example"]["items"]:
            item["deleted_at"] = None
    application.openapi_schema = schema
