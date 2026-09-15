"""Public session failures without credential or account-state details."""

from fastapi.responses import JSONResponse

from mdm.api.errors import problem_response
from mdm.api.schemas import ProblemDetails
from mdm.api.session_cookies import SessionCookies


def invalid_session_response(*, instance: str, cookies: SessionCookies) -> JSONResponse:
    """Expire browser credentials after refresh authentication has been rejected."""
    response = problem_response(
        ProblemDetails(
            type="/problems/invalid-session",
            title="유효하지 않은 로그인 세션",
            status=401,
            detail="유효한 로그인 세션이 없습니다. 다시 로그인해 주세요.",
            code="INVALID_SESSION",
            instance=instance,
        )
    )
    cookies.clear(response)
    return response
