"""Provider-neutral email delivery messages and port."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mdm.domain.auth import EmailAddress
from mdm.domain.credentials import PasswordResetToken, RegistrationToken


class EmailMessageType(StrEnum):
    """Authentication email purposes with separate templates and credentials."""

    REGISTRATION = "REGISTRATION"
    PASSWORD_RESET = "PASSWORD_RESET"


class EmailDeliveryStatus(StrEnum):
    """Sanitized provider outcomes visible to application callers."""

    SENT = "SENT"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"


@dataclass(frozen=True, slots=True, repr=False)
class EmailDeliveryMessage:
    """One purpose-bound email request whose credential is always redacted."""

    message_type: EmailMessageType
    recipient: EmailAddress
    token: RegistrationToken | PasswordResetToken

    def __post_init__(self) -> None:
        expected_token_type = {
            EmailMessageType.REGISTRATION: RegistrationToken,
            EmailMessageType.PASSWORD_RESET: PasswordResetToken,
        }[self.message_type]
        if not isinstance(self.token, expected_token_type):
            raise ValueError("email token purpose does not match message type")

    def __repr__(self) -> str:
        return (
            f"EmailDeliveryMessage(message_type={self.message_type!r}, "
            "recipient=<redacted>, token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    """A provider-neutral outcome without recipient or provider details."""

    status: EmailDeliveryStatus


class EmailSender(Protocol):
    """Awaitable email delivery boundary used after database work completes."""

    async def send(self, message: EmailDeliveryMessage) -> EmailDeliveryResult:
        """Deliver one message and return only a sanitized typed result."""
        ...
