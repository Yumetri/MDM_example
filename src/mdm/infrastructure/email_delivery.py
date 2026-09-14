"""TLS SMTP and deterministic in-memory email delivery adapters."""

from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum
from urllib.parse import urlsplit

import aiosmtplib
from aiosmtplib.errors import SMTPException, SMTPTimeoutError
from aiosmtplib.response import SMTPResponse

from mdm.application.email_delivery import (
    EmailDeliveryMessage,
    EmailDeliveryResult,
    EmailDeliveryStatus,
    EmailMessageType,
)
from mdm.infrastructure.network_values import format_url_host, normalize_network_host
from mdm.infrastructure.settings import Settings

type SmtpSendCommand = Callable[..., Awaitable[tuple[dict[str, SMTPResponse], str]]]


class EmailDeliveryConfigurationError(RuntimeError):
    """Email delivery configuration is incomplete or unsafe."""


class SmtpSecurity(StrEnum):
    """Supported SMTP transports; plaintext delivery is intentionally absent."""

    STARTTLS = "STARTTLS"
    TLS = "TLS"


@dataclass(frozen=True, slots=True, repr=False)
class RenderedEmail:
    """A rendered message kept redacted from diagnostic representations."""

    recipient: str
    subject: str
    body: str

    def __repr__(self) -> str:
        return "RenderedEmail(recipient=<redacted>, subject=<redacted>, body=<redacted>)"


class EmailTemplateRenderer:
    """Render fixed Korean authentication messages with fragment-only tokens."""

    def __init__(self, public_app_base_url: str) -> None:
        self._public_app_origin = _validate_public_app_origin(public_app_base_url)

    def render(self, message: EmailDeliveryMessage) -> RenderedEmail:
        """Render the purpose-specific subject, body, and frontend URL."""
        if message.message_type is EmailMessageType.REGISTRATION:
            subject = "이메일 가입 인증 안내"
            route = "/auth/registration"
            introduction = "아래 링크를 열어 가입을 완료해 주세요."
        else:
            subject = "비밀번호 재설정 안내"
            route = "/auth/password-reset"
            introduction = "아래 링크를 열어 비밀번호를 재설정해 주세요."
        url = f"{self._public_app_origin}{route}#token={message.token.reveal()}"
        return RenderedEmail(
            recipient=message.recipient.value,
            subject=subject,
            body=f"{introduction}\n\n{url}\n",
        )


class InMemoryEmailSender:
    """Deterministic fake that records rendered messages for caller tests."""

    def __init__(
        self,
        *,
        public_app_base_url: str,
        outcomes: Iterable[EmailDeliveryStatus] = (),
    ) -> None:
        self._renderer = EmailTemplateRenderer(public_app_base_url)
        self._outcomes = deque(outcomes)
        self.deliveries: list[RenderedEmail] = []

    async def send(self, message: EmailDeliveryMessage) -> EmailDeliveryResult:
        """Record one rendering and return the next scripted outcome."""
        self.deliveries.append(self._renderer.render(message))
        status = self._outcomes.popleft() if self._outcomes else EmailDeliveryStatus.SENT
        return EmailDeliveryResult(status=status)


class SmtpEmailSender:
    """Deliver email with certificate-validated STARTTLS or implicit TLS."""

    def __init__(
        self,
        *,
        public_app_base_url: str,
        hostname: str,
        port: int,
        username: str,
        password: str,
        sender_address: str,
        timeout_seconds: float,
        security: SmtpSecurity,
        send_command: SmtpSendCommand = aiosmtplib.send,
    ) -> None:
        self._renderer = EmailTemplateRenderer(public_app_base_url)
        try:
            self._hostname = normalize_network_host(hostname)
        except ValueError:
            raise EmailDeliveryConfigurationError(
                "SMTP host must be a hostname or IP literal"
            ) from None
        self._port = port
        self._username = username
        self._password = password
        self._sender_address = sender_address
        self._timeout_seconds = timeout_seconds
        self._security = security
        self._send_command = send_command

    async def send(self, message: EmailDeliveryMessage) -> EmailDeliveryResult:
        """Await the SMTP provider without returning provider or credential details."""
        rendered = self._renderer.render(message)
        email = EmailMessage()
        email["To"] = rendered.recipient
        email["From"] = self._sender_address
        email["Subject"] = rendered.subject
        email.set_content(rendered.body)

        try:
            errors, _ = await self._send_command(
                email,
                sender=self._sender_address,
                recipients=[rendered.recipient],
                hostname=self._hostname,
                port=self._port,
                username=self._username,
                password=self._password,
                timeout=self._timeout_seconds,
                use_tls=self._security is SmtpSecurity.TLS,
                start_tls=self._security is SmtpSecurity.STARTTLS,
                validate_certs=True,
            )
        except (SMTPTimeoutError, TimeoutError):
            return EmailDeliveryResult(status=EmailDeliveryStatus.TIMED_OUT)
        except (SMTPException, OSError):
            return EmailDeliveryResult(status=EmailDeliveryStatus.REJECTED)
        return EmailDeliveryResult(
            status=EmailDeliveryStatus.REJECTED if errors else EmailDeliveryStatus.SENT
        )

    def __repr__(self) -> str:
        return (
            f"SmtpEmailSender(hostname={self._hostname!r}, port={self._port!r}, "
            f"username=<redacted>, password=<redacted>, sender_address=<redacted>, "
            f"timeout_seconds={self._timeout_seconds!r}, security={self._security!r})"
        )


def build_smtp_email_sender(settings: Settings) -> SmtpEmailSender:
    """Build the SMTP adapter or fail without unsafe configuration defaults."""
    required_values = {
        "email_public_app_base_url": settings.email_public_app_base_url,
        "email_smtp_host": settings.email_smtp_host,
        "email_smtp_port": settings.email_smtp_port,
        "email_smtp_username": settings.email_smtp_username,
        "email_smtp_password": settings.email_smtp_password,
        "email_sender_address": settings.email_sender_address,
    }
    if any(value is None for value in required_values.values()):
        raise EmailDeliveryConfigurationError("email delivery configuration is incomplete")

    public_app_base_url = settings.email_public_app_base_url
    hostname = settings.email_smtp_host
    port = settings.email_smtp_port
    username = settings.email_smtp_username
    password = settings.email_smtp_password
    sender_address = settings.email_sender_address
    assert public_app_base_url is not None
    assert hostname is not None
    assert port is not None
    assert username is not None
    assert password is not None
    assert sender_address is not None
    return SmtpEmailSender(
        public_app_base_url=str(public_app_base_url),
        hostname=hostname,
        port=port,
        username=username,
        password=password.get_secret_value(),
        sender_address=str(sender_address),
        timeout_seconds=settings.email_smtp_timeout_seconds,
        security=SmtpSecurity(settings.email_smtp_security),
    )


def _validate_public_app_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise EmailDeliveryConfigurationError(
            "public app URL must be a valid HTTPS origin"
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise EmailDeliveryConfigurationError("public app URL must be an HTTPS origin")
    try:
        hostname = format_url_host(parsed.hostname)
    except ValueError:
        raise EmailDeliveryConfigurationError("public app URL must be an HTTPS origin") from None
    default_port = port in (None, 443)
    authority = hostname if default_port else f"{hostname}:{port}"
    return f"https://{authority}"
