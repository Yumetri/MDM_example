from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

import pytest
from fastapi import Query
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
    assert response.json() == {"status": "ok", "message": "서비스가 실행 중입니다."}


@pytest.mark.api
async def test_readiness_succeeds_when_database_is_available() -> None:
    async with client_for(HealthyReadinessCheck()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "서비스가 요청을 처리할 준비가 되었습니다.",
    }


@pytest.mark.api
async def test_readiness_returns_problem_details_when_database_is_unavailable() -> None:
    async with client_for(UnhealthyReadinessCheck()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://api.example.com/problems/service-unavailable",
        "title": "서비스를 사용할 수 없음",
        "status": 503,
        "detail": "필수 의존 서비스를 사용할 수 없어 현재 요청을 처리할 수 없습니다.",
        "code": "SERVICE_UNAVAILABLE",
        "instance": "/health/ready",
    }


@pytest.mark.api
@pytest.mark.parametrize(
    ("query", "field", "message"),
    [
        ("long_text=ok&count=1", "query.short_text", "필수 필드입니다."),
        (
            "short_text=x&long_text=ok&count=1",
            "query.short_text",
            "허용된 길이보다 짧은 문자열입니다.",
        ),
        (
            "short_text=ok&long_text=too-long&count=1",
            "query.long_text",
            "허용된 길이보다 긴 문자열입니다.",
        ),
        ("short_text=ok&long_text=ok&count=invalid", "query.count", "유효하지 않은 값입니다."),
    ],
)
async def test_validation_errors_are_returned_in_korean(
    query: str,
    field: str,
    message: str,
) -> None:
    application = create_app(readiness_check=HealthyReadinessCheck())

    @application.get("/validation-probe")
    def validation_probe(
        short_text: Annotated[str, Query(min_length=2)],
        long_text: Annotated[str, Query(max_length=4)],
        count: int,
    ) -> dict[str, object]:
        return {"short_text": short_text, "long_text": long_text, "count": count}

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/validation-probe?{query}")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://api.example.com/problems/validation-error",
        "title": "유효하지 않은 요청",
        "status": 422,
        "detail": "하나 이상의 요청 필드가 유효하지 않습니다.",
        "code": "VALIDATION_ERROR",
        "instance": "/validation-probe",
        "violations": [{"field": field, "message": message}],
    }


@pytest.mark.api
async def test_unexpected_errors_are_returned_in_korean() -> None:
    application = create_app(readiness_check=HealthyReadinessCheck())

    @application.get("/error-probe")
    def error_probe() -> None:
        raise RuntimeError("internal detail")

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/error-probe")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://api.example.com/problems/internal-error",
        "title": "서버 내부 오류",
        "status": 500,
        "detail": "서비스에서 예상하지 못한 오류가 발생했습니다.",
        "code": "INTERNAL_ERROR",
        "instance": "/error-probe",
    }
