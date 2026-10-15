from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        sa.UniqueConstraint("email"),
        sa.CheckConstraint("role IN ('user', 'admin')", name="role_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa.true())
    # Bumped on password change/reset; JWTs carrying an older value are rejected.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (
        sa.UniqueConstraint("code"),
        Index("ix_links_owner_id_created_at", "owner_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32))
    target_url: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false())
    click_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthToken(Base):
    """Single-use e-mail verification / password reset tokens (stored hashed)."""

    __tablename__ = "auth_tokens"
    __table_args__ = (sa.UniqueConstraint("token_hash"), Index("ix_auth_tokens_user_id", "user_id"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClickFlushState(Base):
    """Watermark: the last Redis stream entry whose clicks are already counted in `links.click_count`.

    It is updated in the same transaction as the counts, which is what makes click counting exactly-once.
    """

    __tablename__ = "click_flush_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_id: Mapped[str] = mapped_column(String(32))
