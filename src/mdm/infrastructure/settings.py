"""Typed application configuration."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    def reveal_database_url(self) -> str:
        """Return the database URL only at the infrastructure boundary."""
        return self.database_url.get_secret_value()
