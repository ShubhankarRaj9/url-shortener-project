from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuthToken
from app.security import hash_opaque_token, new_opaque_token

VERIFY_EMAIL = "verify_email"
RESET_PASSWORD = "reset_password"


async def issue_token(session: AsyncSession, user_id: uuid.UUID, purpose: str, ttl: timedelta) -> str:
    """Create a fresh token, discarding any older ones for the same user/purpose. Caller commits."""
    raw, digest = new_opaque_token()
    await session.execute(delete(AuthToken).where(AuthToken.user_id == user_id, AuthToken.purpose == purpose))
    session.add(
        AuthToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=digest,
            expires_at=datetime.now(timezone.utc) + ttl,
        )
    )
    return raw


async def consume_token(session: AsyncSession, raw: str, purpose: str) -> uuid.UUID | None:
    """Atomically mark a valid token as used and return its user id. Caller commits."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(AuthToken)
        .where(
            AuthToken.token_hash == hash_opaque_token(raw),
            AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(AuthToken.user_id)
    )
    return result.scalar_one_or_none()
