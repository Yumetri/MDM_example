"""FastAPI composition root."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.api.documentation import build_documentation_router
from mdm.api.errors import (
    readiness_unavailable_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from mdm.api.health import build_health_router
from mdm.api.openapi import configure_openapi
from mdm.application.health import CheckReadiness, ReadinessCheck, ReadinessUnavailable
from mdm.infrastructure.database import SqlAlchemyDatabaseProbe, create_engine
from mdm.infrastructure.settings import Settings


def create_app(readiness_check: ReadinessCheck | None = None) -> FastAPI:
    """Assemble the API, application services, and infrastructure adapters."""
    engine: AsyncEngine | None = None
    if readiness_check is None:
        settings = Settings()
        engine = create_engine(settings.reveal_database_url())
        readiness_check = CheckReadiness(SqlAlchemyDatabaseProbe(engine))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    application = FastAPI(
        title="MDM API",
        summary="Dimension 기반 마스터 데이터 관리",
        description=(
            "Dimension을 참조해 고유 코드를 생성하는 마스터 데이터를 관리하는 서비스 API입니다."
        ),
        version="0.1.0",
        openapi_tags=[
            {
                "name": "Health",
                "description": "서비스의 실행 상태와 요청 처리 준비 상태를 각각 확인합니다.",
            }
        ],
        docs_url=None,
        lifespan=lifespan,
        redoc_url=None,
    )
    application.add_exception_handler(
        ReadinessUnavailable,
        readiness_unavailable_handler,
    )
    application.add_exception_handler(
        RequestValidationError,
        validation_error_handler,
    )
    application.add_exception_handler(Exception, unexpected_error_handler)
    application.include_router(build_health_router(readiness_check))
    application.include_router(build_documentation_router(application))
    configure_openapi(application)
    return application


app = create_app()
