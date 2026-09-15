from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from mdm.api.change_requests import (
    CHANGE_REQUEST_ERRORS,
    build_change_request_router,
    change_request_error_handler,
)
from mdm.api.errors import authorization_denied_handler, validation_error_handler
from mdm.application.auth import HumanPrincipal
from mdm.application.authorization import AuthorizationDenied, AuthorizationPolicy
from mdm.domain.auth import UserRole
from mdm.domain.change_requests import ChangeRequest

pytestmark = pytest.mark.api
USER_ID = UUID("00000000-0000-7000-8000-000000000001")
REQUEST_ID = UUID("00000000-0000-7000-8000-000000000002")
BASE = "/api/v1/master-code-change-requests"


class SubmissionUseCases:
    def __init__(self):
        self.calls = []

    async def submit(self, principal, proposal, *, reason):
        self.calls.append(proposal)
        return ChangeRequest(
            REQUEST_ID, proposal, principal.user_id, datetime(2026, 9, 15, tzinfo=UTC)
        )


def _app(role=UserRole.USER):
    service = SubmissionUseCases()
    app = FastAPI()
    app.include_router(
        build_change_request_router(
            use_cases=service,  # type: ignore[bad-argument-type]
            principal_dependency=lambda: HumanPrincipal(USER_ID, role),
            authorization=AuthorizationPolicy(),
        )
    )
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(AuthorizationDenied, authorization_denied_handler)
    for exception in CHANGE_REQUEST_ERRORS:
        app.add_exception_handler(exception, change_request_error_handler)
    return app, service


async def test_submit_contract_returns_only_request_identity_state_and_time():
    app, service = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            BASE,
            json={
                "proposal": {
                    "operation": "DELETE",
                    "target_id": str(REQUEST_ID),
                    "expected_etag": '"mc-1-' + "0" * 64 + '"',
                    "payload": None,
                }
            },
        )
    assert result.status_code == 201, result.text
    assert result.json() == {
        "id": str(REQUEST_ID),
        "status": "PENDING",
        "created_at": "2026-09-15T00:00:00Z",
    }
    assert len(service.calls) == 1


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
async def test_admin_submission_is_forbidden_before_use_case(role):
    app, service = _app(role)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            BASE,
            json={
                "proposal": {
                    "operation": "DELETE",
                    "target_id": str(REQUEST_ID),
                    "expected_etag": '"mc-1-' + "0" * 64 + '"',
                }
            },
        )
    assert result.status_code == 403
    assert result.json()["code"] == "AUTHORIZATION_DENIED"
    assert service.calls == []
