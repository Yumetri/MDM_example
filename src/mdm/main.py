"""FastAPI composition root."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from mdm.api.auth import build_human_principal_dependency
from mdm.api.dimensions import build_company_router
from mdm.api.documentation import build_documentation_router
from mdm.api.errors import (
    authorization_denied_handler,
    company_conflict_handler,
    company_not_found_handler,
    company_repository_unavailable_handler,
    dimension_validation_error_handler,
    invalid_access_token_handler,
    numeric_dimension_conflict_handler,
    numeric_dimension_not_found_handler,
    numeric_dimension_repository_unavailable_handler,
    readiness_unavailable_handler,
    string_dimension_conflict_handler,
    string_dimension_not_found_handler,
    string_dimension_repository_unavailable_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from mdm.api.health import build_health_router
from mdm.api.numeric_dimensions import build_network_router, build_year_router
from mdm.api.openapi import configure_openapi
from mdm.api.string_dimensions import (
    build_brand_router,
    build_category_router,
    build_country_router,
    build_model_router,
)
from mdm.application.audit import HumanMutationAuditFactory
from mdm.application.auth import AuthenticateHumanPrincipal, InvalidAccessToken
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.application.dimensions import (
    CompanyCodeConflict,
    CompanyMultipleConflicts,
    CompanyNotFound,
    CompanyRepositoryUnavailable,
    CompanyValueConflict,
    CreateCompany,
    GetCompany,
    ListCompanies,
)
from mdm.application.health import CheckReadiness, ReadinessCheck, ReadinessUnavailable
from mdm.application.numeric_dimensions import (
    CreateNetwork,
    CreateYear,
    GetNetwork,
    GetYear,
    ListNetworks,
    ListYears,
    NumericDimensionCodeConflict,
    NumericDimensionMultipleConflicts,
    NumericDimensionNotFound,
    NumericDimensionRepositoryUnavailable,
    NumericDimensionValueConflict,
)
from mdm.application.string_dimensions import (
    CreateBrand,
    CreateCategory,
    CreateCountry,
    CreateModel,
    GetBrand,
    GetCategory,
    GetCountry,
    GetModel,
    ListBrands,
    ListCategories,
    ListCountries,
    ListModels,
    StringDimensionCodeConflict,
    StringDimensionMultipleConflicts,
    StringDimensionNotFound,
    StringDimensionRepositoryUnavailable,
    StringDimensionValueConflict,
)
from mdm.domain.dimensions import DimensionValidationError
from mdm.infrastructure.database import (
    SqlAlchemyDatabaseProbe,
    create_engine,
    create_session_factory,
)
from mdm.infrastructure.jwt import RejectingAccessTokenVerifier, build_access_jwt_codec
from mdm.infrastructure.operational_events import JsonLineOperationalEventSink
from mdm.infrastructure.repositories.companies import SqlAlchemyCompanyRepository
from mdm.infrastructure.repositories.numeric_dimensions import (
    SqlAlchemyNetworkRepository,
    SqlAlchemyYearRepository,
)
from mdm.infrastructure.repositories.string_dimensions import (
    SqlAlchemyBrandRepository,
    SqlAlchemyCategoryRepository,
    SqlAlchemyCountryRepository,
    SqlAlchemyModelRepository,
)
from mdm.infrastructure.settings import Settings
from mdm.infrastructure.uuid7 import Uuid7Generator


def create_app(readiness_check: ReadinessCheck | None = None) -> FastAPI:
    """Assemble the API, application services, and infrastructure adapters."""
    engine: AsyncEngine | None = None
    company_router = None
    string_dimension_routers = []
    numeric_dimension_routers = []
    if readiness_check is None:
        settings = Settings()
        engine = create_engine(settings.reveal_database_url())
        readiness_check = CheckReadiness(SqlAlchemyDatabaseProbe(engine))
        session_factory = create_session_factory(engine)
        repository = SqlAlchemyCompanyRepository(session_factory)
        if (
            settings.auth_jwt_active_kid is None
            and settings.auth_jwt_private_key_path is None
            and settings.auth_jwt_jwks_path is None
        ):
            verifier = RejectingAccessTokenVerifier()
        else:
            verifier = build_access_jwt_codec(settings)
        authenticate = AuthenticateHumanPrincipal(
            verifier,
            JsonLineOperationalEventSink(destination="stderr"),
            clock=lambda: datetime.now(UTC),
        )
        authorization = AuthorizationPolicy()
        principal_dependency = build_human_principal_dependency(authenticate)
        audit_factory = HumanMutationAuditFactory(change_set_ids=Uuid7Generator().new)
        company_router = build_company_router(
            create_company=CreateCompany(
                repository,
                authorization,
                audit_factory,
            ),
            get_company=GetCompany(repository, authorization),
            list_companies=ListCompanies(repository, authorization),
            principal_dependency=principal_dependency,
            authorization=authorization,
        )
        model_repository = SqlAlchemyModelRepository(session_factory)
        string_dimension_routers.append(
            build_model_router(
                create_dimension=CreateModel(model_repository, authorization, audit_factory),
                get_dimension=GetModel(model_repository, authorization),
                list_dimensions=ListModels(model_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )
        brand_repository = SqlAlchemyBrandRepository(session_factory)
        string_dimension_routers.append(
            build_brand_router(
                create_dimension=CreateBrand(brand_repository, authorization, audit_factory),
                get_dimension=GetBrand(brand_repository, authorization),
                list_dimensions=ListBrands(brand_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )
        country_repository = SqlAlchemyCountryRepository(session_factory)
        string_dimension_routers.append(
            build_country_router(
                create_dimension=CreateCountry(country_repository, authorization, audit_factory),
                get_dimension=GetCountry(country_repository, authorization),
                list_dimensions=ListCountries(country_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )
        category_repository = SqlAlchemyCategoryRepository(session_factory)
        string_dimension_routers.append(
            build_category_router(
                create_dimension=CreateCategory(category_repository, authorization, audit_factory),
                get_dimension=GetCategory(category_repository, authorization),
                list_dimensions=ListCategories(category_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )
        year_repository = SqlAlchemyYearRepository(session_factory)
        numeric_dimension_routers.append(
            build_year_router(
                create_dimension=CreateYear(year_repository, authorization, audit_factory),
                get_dimension=GetYear(year_repository, authorization),
                list_dimensions=ListYears(year_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )
        network_repository = SqlAlchemyNetworkRepository(session_factory)
        numeric_dimension_routers.append(
            build_network_router(
                create_dimension=CreateNetwork(network_repository, authorization, audit_factory),
                get_dimension=GetNetwork(network_repository, authorization),
                list_dimensions=ListNetworks(network_repository, authorization),
                principal_dependency=principal_dependency,
                authorization=authorization,
            )
        )

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
        ],
        docs_url=None,
        lifespan=lifespan,
        redoc_url=None,
    )
    application.add_exception_handler(
        InvalidAccessToken,
        invalid_access_token_handler,
    )
    application.add_exception_handler(
        AuthorizationDenied,
        authorization_denied_handler,
    )
    application.add_exception_handler(
        DimensionValidationError,
        dimension_validation_error_handler,
    )
    application.add_exception_handler(CompanyNotFound, company_not_found_handler)
    application.add_exception_handler(
        CompanyRepositoryUnavailable, company_repository_unavailable_handler
    )
    for conflict_type in (
        CompanyCodeConflict,
        CompanyValueConflict,
        CompanyMultipleConflicts,
    ):
        application.add_exception_handler(conflict_type, company_conflict_handler)
    application.add_exception_handler(StringDimensionNotFound, string_dimension_not_found_handler)
    application.add_exception_handler(
        StringDimensionRepositoryUnavailable,
        string_dimension_repository_unavailable_handler,
    )
    for conflict_type in (
        StringDimensionCodeConflict,
        StringDimensionValueConflict,
        StringDimensionMultipleConflicts,
    ):
        application.add_exception_handler(conflict_type, string_dimension_conflict_handler)
    application.add_exception_handler(NumericDimensionNotFound, numeric_dimension_not_found_handler)
    application.add_exception_handler(
        NumericDimensionRepositoryUnavailable,
        numeric_dimension_repository_unavailable_handler,
    )
    for conflict_type in (
        NumericDimensionCodeConflict,
        NumericDimensionValueConflict,
        NumericDimensionMultipleConflicts,
    ):
        application.add_exception_handler(conflict_type, numeric_dimension_conflict_handler)
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
    if company_router is not None:
        application.include_router(company_router)
    for router in string_dimension_routers:
        application.include_router(router)
    for router in numeric_dimension_routers:
        application.include_router(router)
    application.include_router(build_documentation_router(application))
    configure_openapi(application)
    return application


app = create_app()
