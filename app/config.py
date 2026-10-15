from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (see .env.example)."""

    # Empty variables (e.g. `ADMIN_EMAIL=` coming from docker compose) count as "not set".
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    app_name: str = "URL Shortener"
    environment: Literal["development", "test", "production"] = "production"
    log_level: str = "INFO"
    log_json: bool = True
    docs_enabled: bool = True

    # Storage
    database_url: str
    db_pool_size: int = Field(10, ge=1)
    db_max_overflow: int = Field(10, ge=0)
    redis_url: str

    # Auth
    secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "url-shortener"
    access_token_expire_minutes: int = Field(30, ge=1, le=1440)
    verify_token_ttl_hours: int = Field(24, ge=1)
    reset_token_ttl_minutes: int = Field(30, ge=5)
    bcrypt_rounds: int = Field(12, ge=4, le=16)
    login_max_failures: int = Field(5, ge=1)
    login_lockout_seconds: int = Field(900, ge=60)

    # URLs
    public_base_url: str = "http://localhost:8000"  # prefix of generated short links
    frontend_url: str | None = None  # where e-mail links point; defaults to public_base_url

    # Seeded admin (only used when the users table is empty)
    admin_email: EmailStr | None = None
    admin_password: SecretStr | None = None

    # E-mail
    email_backend: Literal["smtp", "log", "memory"] = "log"
    email_from: str = "URL Shortener <no-reply@example.com>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_security: Literal["starttls", "tls", "none"] = "starttls"

    # Links / caching
    short_code_length: int = Field(7, ge=4, le=16)
    max_links_per_user: int = Field(1000, ge=1)
    cache_ttl_seconds: int = Field(3600, ge=1)
    negative_cache_ttl_seconds: int = Field(60, ge=1)
    click_flush_interval_seconds: float = Field(5.0, gt=0)

    @field_validator("public_base_url", "frontend_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @field_validator("admin_password")
    @classmethod
    def _admin_password_strength(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and v.get_secret_value() and len(v.get_secret_value()) < 12:
            raise ValueError("ADMIN_PASSWORD must be at least 12 characters")
        return v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        if self.environment == "production":
            if "change_me" in secret.lower():
                raise ValueError("SECRET_KEY still has its placeholder value")
            if self.email_backend == "memory":
                raise ValueError("EMAIL_BACKEND=memory is not allowed in production")
        return self

    @property
    def public_host(self) -> str:
        return (urlsplit(self.public_base_url).hostname or "").lower()

    @property
    def web_url(self) -> str:
        return self.frontend_url or self.public_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
