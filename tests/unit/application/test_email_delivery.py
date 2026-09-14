import pytest

from mdm.application.email_delivery import (
    EmailDeliveryMessage,
    EmailMessageType,
)
from mdm.domain.auth import EmailAddress
from mdm.domain.credentials import PasswordResetToken, RegistrationToken

REGISTRATION_TOKEN = RegistrationToken("A" * 43)
PASSWORD_RESET_TOKEN = PasswordResetToken("A" * 43)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message_type", "token"),
    [
        (EmailMessageType.REGISTRATION, PASSWORD_RESET_TOKEN),
        (EmailMessageType.PASSWORD_RESET, REGISTRATION_TOKEN),
    ],
)
def test_email_delivery_message_rejects_a_token_for_another_purpose(
    message_type: EmailMessageType,
    token: RegistrationToken | PasswordResetToken,
) -> None:
    with pytest.raises(ValueError, match="token purpose"):
        EmailDeliveryMessage(
            message_type=message_type,
            recipient=EmailAddress("user@example.net"),
            token=token,
        )


@pytest.mark.unit
def test_email_delivery_message_repr_redacts_recipient_and_token() -> None:
    message = EmailDeliveryMessage(
        message_type=EmailMessageType.REGISTRATION,
        recipient=EmailAddress("secret@example.net"),
        token=REGISTRATION_TOKEN,
    )

    representation = repr(message)

    assert "secret@example.net" not in representation
    assert REGISTRATION_TOKEN.reveal() not in representation
    assert "<redacted>" in representation
