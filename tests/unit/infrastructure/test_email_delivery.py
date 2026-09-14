import asyncio
from email.message import EmailMessage
from typing import Any

import pytest
from aiosmtplib.response import SMTPResponse

from mdm.application.email_delivery import (
    EmailDeliveryMessage,
    EmailDeliveryStatus,
    EmailMessageType,
)
from mdm.domain.auth import EmailAddress
from mdm.domain.credentials import PasswordResetToken, RegistrationToken
from mdm.infrastructure.email_delivery import (
    EmailDeliveryConfigurationError,
    EmailTemplateRenderer,
    InMemoryEmailSender,
    SmtpEmailSender,
    SmtpSecurity,
    build_smtp_email_sender,
)
from mdm.infrastructure.settings import Settings

REGISTRATION_TOKEN = RegistrationToken("A" * 43)
PASSWORD_RESET_TOKEN = PasswordResetToken("A" * 43)


def _message(
    message_type: EmailMessageType = EmailMessageType.REGISTRATION,
) -> EmailDeliveryMessage:
    token = (
        REGISTRATION_TOKEN
        if message_type is EmailMessageType.REGISTRATION
        else PASSWORD_RESET_TOKEN
    )
    return EmailDeliveryMessage(
        message_type=message_type,
        recipient=EmailAddress("user@example.net"),
        token=token,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message_type", "route", "token"),
    [
        (EmailMessageType.REGISTRATION, "/auth/registration", REGISTRATION_TOKEN),
        (EmailMessageType.PASSWORD_RESET, "/auth/password-reset", PASSWORD_RESET_TOKEN),
    ],
)
def test_renderer_puts_raw_token_only_in_the_frontend_fragment(
    message_type: EmailMessageType,
    route: str,
    token: RegistrationToken | PasswordResetToken,
) -> None:
    rendered = EmailTemplateRenderer("https://app.example.net").render(_message(message_type))

    expected_url = f"https://app.example.net{route}#token={token.reveal()}"
    assert expected_url in rendered.body
    assert "?token=" not in rendered.body
    assert token.reveal() not in rendered.subject
    assert token.reveal() not in rendered.recipient
    assert token.reveal() not in repr(rendered)
    assert rendered.recipient == "user@example.net"
    assert rendered.subject


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "http://app.example.net",
        "https://user@app.example.net",
        "https://app.example.net/path",
        "https://app.example.net?query=value",
        "https://app.example.net#fragment",
        "https://./",
        "https://bad..example/",
        "https://-bad.example/",
    ],
)
def test_renderer_rejects_a_public_app_url_that_is_not_an_https_origin(
    base_url: str,
) -> None:
    with pytest.raises(EmailDeliveryConfigurationError):
        EmailTemplateRenderer(base_url)


@pytest.mark.unit
async def test_in_memory_sender_records_rendered_messages_and_scripted_results() -> None:
    sender = InMemoryEmailSender(
        public_app_base_url="https://app.example.net",
        outcomes=[EmailDeliveryStatus.REJECTED, EmailDeliveryStatus.SENT],
    )

    rejected = await sender.send(_message())
    sent = await sender.send(_message(EmailMessageType.PASSWORD_RESET))

    assert rejected.status is EmailDeliveryStatus.REJECTED
    assert sent.status is EmailDeliveryStatus.SENT
    assert len(sender.deliveries) == 2
    assert "/auth/registration#token=" in sender.deliveries[0].body
    assert "/auth/password-reset#token=" in sender.deliveries[1].body


@pytest.mark.unit
async def test_smtp_sender_uses_required_starttls_and_returns_sent() -> None:
    calls: list[tuple[EmailMessage, dict[str, Any]]] = []

    async def send_command(
        message: EmailMessage, **kwargs: Any
    ) -> tuple[dict[str, SMTPResponse], str]:
        calls.append((message, kwargs))
        await asyncio.sleep(0)
        return {}, "accepted"

    sender = SmtpEmailSender(
        public_app_base_url="https://app.example.net",
        hostname="smtp.example.net",
        port=587,
        username="smtp-user",
        password="smtp-secret",
        sender_address="noreply@example.net",
        timeout_seconds=5.0,
        security=SmtpSecurity.STARTTLS,
        send_command=send_command,
    )

    result = await sender.send(_message())

    assert result.status is EmailDeliveryStatus.SENT
    email, kwargs = calls[0]
    assert email["To"] == "user@example.net"
    assert email["From"] == "noreply@example.net"
    assert kwargs["start_tls"] is True
    assert kwargs["use_tls"] is False
    assert kwargs["validate_certs"] is True
    assert kwargs["password"] == "smtp-secret"
    assert kwargs["timeout"] == 5.0


@pytest.mark.unit
async def test_smtp_sender_uses_implicit_tls_without_starttls() -> None:
    calls: list[dict[str, Any]] = []

    async def send_command(
        message: EmailMessage, **kwargs: Any
    ) -> tuple[dict[str, SMTPResponse], str]:
        calls.append(kwargs)
        return {}, "accepted"

    sender = SmtpEmailSender(
        public_app_base_url="https://app.example.net",
        hostname="smtp.example.net",
        port=465,
        username="smtp-user",
        password="smtp-secret",
        sender_address="noreply@example.net",
        timeout_seconds=5.0,
        security=SmtpSecurity.TLS,
        send_command=send_command,
    )

    await sender.send(_message())

    assert calls[0]["use_tls"] is True
    assert calls[0]["start_tls"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ({"user@example.net": SMTPResponse(550, "rejected")}, "rejected"),
            EmailDeliveryStatus.REJECTED,
        ),
        (TimeoutError("provider detail"), EmailDeliveryStatus.TIMED_OUT),
        (OSError("provider detail"), EmailDeliveryStatus.REJECTED),
    ],
)
async def test_smtp_sender_returns_only_sanitized_typed_failures(
    result: tuple[dict[str, SMTPResponse], str] | Exception,
    expected: EmailDeliveryStatus,
) -> None:
    async def send_command(
        message: EmailMessage, **kwargs: Any
    ) -> tuple[dict[str, SMTPResponse], str]:
        if isinstance(result, Exception):
            raise result
        return result

    sender = SmtpEmailSender(
        public_app_base_url="https://app.example.net",
        hostname="smtp.example.net",
        port=587,
        username="smtp-user",
        password="smtp-secret",
        sender_address="noreply@example.net",
        timeout_seconds=5.0,
        security=SmtpSecurity.STARTTLS,
        send_command=send_command,
    )

    outcome = await sender.send(_message())

    assert outcome.status is expected
    assert "provider detail" not in repr(outcome)


@pytest.mark.unit
async def test_smtp_sender_does_not_block_other_event_loop_work() -> None:
    sender_reached_await = asyncio.Event()
    let_sender_finish = asyncio.Event()
    other_work_completed = asyncio.Event()

    async def send_command(
        message: EmailMessage, **kwargs: Any
    ) -> tuple[dict[str, SMTPResponse], str]:
        sender_reached_await.set()
        await let_sender_finish.wait()
        return {}, "accepted"

    sender = SmtpEmailSender(
        public_app_base_url="https://app.example.net",
        hostname="smtp.example.net",
        port=587,
        username="smtp-user",
        password="smtp-secret",
        sender_address="noreply@example.net",
        timeout_seconds=5.0,
        security=SmtpSecurity.STARTTLS,
        send_command=send_command,
    )

    send_task = asyncio.create_task(sender.send(_message()))
    other_work_task = asyncio.create_task(_mark_completed(other_work_completed))
    await sender_reached_await.wait()
    await other_work_task
    let_sender_finish.set()
    await send_task

    assert other_work_completed.is_set()


@pytest.mark.unit
def test_builder_rejects_incomplete_smtp_configuration_without_values() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
        _env_file=None,
    )

    with pytest.raises(EmailDeliveryConfigurationError) as caught:
        build_smtp_email_sender(settings)

    assert "configuration is incomplete" in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.unit
def test_builder_uses_validated_settings_without_exposing_the_password() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost/mdm",
        email_public_app_base_url="https://app.example.net",
        email_smtp_host="smtp.example.net",
        email_smtp_port=465,
        email_smtp_security="TLS",
        email_smtp_username="smtp-user",
        email_smtp_password="smtp-password",
        email_sender_address="noreply@example.net",
        email_smtp_timeout_seconds=4.0,
        _env_file=None,
    )

    sender = build_smtp_email_sender(settings)

    assert "smtp-password" not in repr(sender)


async def _mark_completed(event: asyncio.Event) -> None:
    await asyncio.sleep(0)
    event.set()
