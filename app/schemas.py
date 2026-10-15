from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Generic, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

T = TypeVar("T")

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MAX_URL_LENGTH = 2048


def _check_password(v: str) -> str:
    if len(v) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(v.encode("utf-8")) > 72:  # bcrypt only uses the first 72 bytes
        raise ValueError("Password must be at most 72 bytes")
    if not v.strip():
        raise ValueError("Password must not be blank")
    return v


class MessageOut(BaseModel):
    message: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(json_schema_extra={"format": "password"})
    full_name: str | None = Field(default=None, max_length=100)

    _pw = field_validator("password")(_check_password)


class TokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(TokenRequest):
    new_password: str = Field(json_schema_extra={"format": "password"})

    _pw = field_validator("new_password")(_check_password)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(json_schema_extra={"format": "password"})

    _pw = field_validator("new_password")(_check_password)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: str
    is_verified: bool
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class LinkCreate(BaseModel):
    url: str = Field(description="Destination; must be an http(s) URL", examples=["https://example.org/some/long/path"])
    alias: str | None = Field(
        default=None,
        description="Optional custom short code: 3-32 chars of letters, digits, '_' or '-'",
        examples=["my-link"],
    )

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if len(v) > MAX_URL_LENGTH:
            raise ValueError(f"URL must be at most {MAX_URL_LENGTH} characters")
        if any(ord(c) < 33 or ord(c) == 127 for c in v):
            raise ValueError("URL must not contain whitespace or control characters")
        parts = urlsplit(v)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("URL must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("URLs with embedded credentials are not allowed")
        try:
            parts.port  # noqa: B018 - raises on an invalid port
        except ValueError as exc:
            raise ValueError("URL has an invalid port") from exc
        return v

    @field_validator("alias")
    @classmethod
    def _alias(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not ALIAS_RE.fullmatch(v):
            raise ValueError("Alias must be 3-32 characters: letters, digits, '_' or '-'")
        return v


class LinkOut(BaseModel):
    id: int
    code: str
    short_url: str
    target_url: str
    is_custom: bool
    click_count: int
    last_clicked_at: datetime | None
    created_at: datetime
    owner_id: uuid.UUID


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str
    version: str
    checks: dict[str, str]
