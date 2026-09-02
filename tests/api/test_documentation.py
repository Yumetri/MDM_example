from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from mdm.main import create_app


class HealthyReadinessCheck:
    async def execute(self) -> None:
        return None


@asynccontextmanager
async def documentation_client() -> AsyncGenerator[tuple[AsyncClient, dict[str, object]]]:
    application = create_app(readiness_check=HealthyReadinessCheck())
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, application.openapi()


@pytest.mark.api
async def test_docs_serves_scalar_for_the_existing_openapi_document() -> None:
    async with documentation_client() as (client, _):
        response = await client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "@scalar/api-reference" in response.text
    assert '"url": "/openapi.json"' in response.text
    assert "MDM API - Scalar API 문서" in response.text
    assert "Swagger UI" not in response.text


@pytest.mark.api
async def test_openapi_json_path_is_unchanged() -> None:
    async with documentation_client() as (client, expected_schema):
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json() == expected_schema


@pytest.mark.api
async def test_redoc_is_not_exposed() -> None:
    async with documentation_client() as (client, _):
        response = await client.get("/redoc")

    assert response.status_code == 404
