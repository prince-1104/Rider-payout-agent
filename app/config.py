"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every field maps to an upper-case environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "QuickDrop Rider Payout Dispute Desk"
    database_url: str = Field(
        default="postgresql+psycopg://quickdrop:quickdrop@localhost:5432/quickdrop",
        description="SQLAlchemy database URL (PostgreSQL in all deployed environments).",
    )
    payswift_base_url: str = Field(default="http://localhost:8081")
    log_level: str = Field(default="INFO")
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=2.0, gt=0, le=3)

    @field_validator("database_url")
    @classmethod
    def _normalise_postgres_driver(cls, value: str) -> str:
        """Route plain ``postgres://`` / ``postgresql://`` URLs to the psycopg v3 driver."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @field_validator("payswift_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
