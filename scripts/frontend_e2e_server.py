"""Loopback-only test assembly. Never imported by production mdm entrypoints."""

import hmac
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import uvicorn
from fastapi import HTTPException, Request
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import update
from sqlalchemy.engine import make_url

from mdm.infrastructure.database import create_engine, create_session_factory
from mdm.infrastructure.email_delivery import InMemoryEmailSender
from mdm.infrastructure.models import RegistrationChallengeRecord
from mdm.infrastructure.settings import Settings
from mdm.main import create_app


class BrowserTestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MDM_E2E_", hide_input_in_errors=True)
    api_port: int = Field(ge=1, le=65535)
    control_key: SecretStr = Field(min_length=32)
    origin: str


def main() -> None:
    test = BrowserTestSettings()
    settings = Settings()
    database = make_url(settings.reveal_database_url())
    if (
        database.database != "mdm_test"
        or database.host not in {"localhost", "127.0.0.1", "::1"}
        or database.drivername != "postgresql+asyncpg"
    ):
        raise ValueError("browser tests require a local mdm_test database")
    sender = InMemoryEmailSender(public_app_base_url=test.origin)
    with patch("mdm.auth_sessions.build_smtp_email_sender", return_value=sender):
        app = create_app()

    def authorize(request: Request) -> None:
        supplied = request.headers.get("X-E2E-Key", "")
        if not hmac.compare_digest(supplied, test.control_key.get_secret_value()):
            raise HTTPException(status_code=403)

    @app.get("/__e2e__/mail", include_in_schema=False)
    def mail(request: Request, email: str):
        authorize(request)
        for message in reversed(sender.deliveries):
            if message.recipient == email:
                link = next(
                    line for line in message.body.splitlines() if line.startswith("https://")
                )
                return {"link": link}
        raise HTTPException(status_code=404)

    @app.post("/__e2e__/expire-registration", include_in_schema=False)
    async def expire_registration(request: Request, email: str):
        authorize(request)
        # Restrict fixture mutation to a synthetic recipient issued by this test process.
        if not any(message.recipient == email for message in sender.deliveries):
            raise HTTPException(status_code=404)
        engine = create_engine(settings.reveal_database_url())
        try:
            now = datetime.now(UTC)
            async with create_session_factory(engine).begin() as session:
                await session.execute(
                    update(RegistrationChallengeRecord)
                    .where(
                        RegistrationChallengeRecord.normalized_email == email,
                        RegistrationChallengeRecord.status == "ACTIVE",
                    )
                    .values(
                        created_at=now - timedelta(days=2), expires_at=now - timedelta(seconds=1)
                    )
                )
        finally:
            await engine.dispose()
        return {"status": "expired"}

    uvicorn.run(app, host="127.0.0.1", port=test.api_port, access_log=False, log_level="error")


if __name__ == "__main__":
    main()
