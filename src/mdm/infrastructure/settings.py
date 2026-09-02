"""Typed application configuration."""

from pydantic import SecretStr
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

    def reveal_database_url(self) -> str:
        """Return the database URL only at the infrastructure boundary."""
        return self.database_url.get_secret_value()
