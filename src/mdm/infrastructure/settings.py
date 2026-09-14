"""Typed application configuration."""

from pathlib import Path
from typing import Literal

from pydantic import EmailStr, Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mdm.infrastructure.network_values import normalize_network_host


class Settings(BaseSettings):
    """Configuration loaded from MDM-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MDM_",
        extra="ignore",
        frozen=True,
    )

    database_url: SecretStr
    auth_password_hash_memory_mib: int = Field(default=19, ge=1)
    auth_password_hash_iterations: int = Field(default=2, ge=1)
    auth_password_hash_parallelism: int = Field(default=1, ge=1)
    auth_password_hash_max_concurrency: int = Field(default=2, ge=1, le=8)
    auth_password_hash_acquire_timeout_seconds: float = Field(default=2.0, ge=0.1, le=10)
    auth_jwt_active_kid: str | None = None
    auth_jwt_private_key_path: Path | None = None
    auth_jwt_jwks_path: Path | None = None
    email_public_app_base_url: HttpUrl | None = None
    email_smtp_host: str | None = None
    email_smtp_port: int | None = Field(default=None, ge=1, le=65535)
    email_smtp_security: Literal["STARTTLS", "TLS"] = "STARTTLS"
    email_smtp_username: str | None = Field(default=None, min_length=1)
    email_smtp_password: SecretStr | None = Field(default=None, min_length=1)
    email_sender_address: EmailStr | None = None
    email_smtp_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    @field_validator("email_smtp_host")
    @classmethod
    def validate_email_smtp_host(cls, value: str | None) -> str | None:
        """Accept an IP literal or normalize one valid DNS hostname."""
        if value is None:
            return None
        try:
            return normalize_network_host(value)
        except ValueError:
            raise ValueError("email SMTP host must be a hostname or IP literal") from None

    @model_validator(mode="after")
    def validate_email_public_app_origin(self) -> "Settings":
        """Require an HTTPS origin rather than a path-bearing public URL."""
        url = self.email_public_app_base_url
        host = url.host if url is not None else None
        if url is not None and (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or host is None
            or url.path not in ("", "/")
            or url.query is not None
            or url.fragment is not None
        ):
            raise ValueError("email public app base URL must be an HTTPS origin")
        if host is not None:
            try:
                normalize_network_host(host)
            except ValueError:
                raise ValueError("email public app base URL must be an HTTPS origin") from None
        return self

    def reveal_database_url(self) -> str:
        """Return the database URL only at the infrastructure boundary."""
        return self.database_url.get_secret_value()
