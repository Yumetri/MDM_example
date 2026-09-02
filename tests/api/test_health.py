from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from mdm.application.health import ReadinessCheck, ReadinessUnavailable
from mdm.main import create_app


class HealthyReadinessCheck:
    async def execute(self) -> None:
        return None


class UnhealthyReadinessCheck:
    async def execute(self) -> None:
        raise ReadinessUnavailable


@asynccontextmanager
async def client_for(readiness_check: ReadinessCheck) -> AsyncGenerator[AsyncClient]:
    app = create_app(readiness_check=readiness_check)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.api
async def test_liveness_does_not_depend_on_database() -> None:
    async with client_for(UnhealthyReadinessCheck()) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "The service is running."}


@pytest.mark.api
async def test_readiness_succeeds_when_database_is_available() -> None:
    async with client_for(HealthyReadinessCheck()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "The service is ready."}


@pytest.mark.api
async def test_readiness_returns_problem_details_when_database_is_unavailable() -> None:
    async with client_for(UnhealthyReadinessCheck()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://api.example.com/problems/service-unavailable",
        "title": "Service unavailable",
        "status": 503,
        "detail": "The service cannot accept traffic because a required dependency is unavailable.",
        "code": "SERVICE_UNAVAILABLE",
        "instance": "/health/ready",
    }
