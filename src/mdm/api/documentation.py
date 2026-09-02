"""Interactive API documentation routes."""

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference


def build_documentation_router(application: FastAPI) -> APIRouter:
    """Build the Scalar UI route for the application's OpenAPI document."""
    router = APIRouter(tags=["Documentation"])

    @router.get(
        "/docs",
        include_in_schema=False,
        operation_id="get_api_documentation",
        response_class=HTMLResponse,
        response_model=None,
        status_code=status.HTTP_200_OK,
        summary="View interactive API documentation",
        description="Render the MDM API's OpenAPI document with Scalar.",
    )
    def get_api_documentation() -> HTMLResponse:
        return get_scalar_api_reference(
            openapi_url=application.openapi_url,
            title=f"{application.title} - Scalar API Reference",
        )

    return router
