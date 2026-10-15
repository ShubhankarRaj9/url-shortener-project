from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import Settings


def _hash(plain: str, rounds: int) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("ascii")


def _verify(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:  # malformed hash / >72 bytes
        return False


# bcrypt is deliberately slow, so keep it off the event loop.
async def hash_password(settings: Settings, plain: str) -> str:
    return await asyncio.to_thread(_hash, plain, settings.bcrypt_rounds)


async def verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify, plain, hashed)


def new_opaque_token() -> tuple[str, str]:
    """Return (raw token to send to the user, sha256 hex digest to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_opaque_token(raw)


def hash_opaque_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_access_token(settings: Settings, *, user_id: str, token_version: int) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    expires = timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "ver": token_version,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + expires,
    }
    token = jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, int(expires.total_seconds())


def decode_access_token(settings: Settings, token: str) -> dict:
    return jwt.decode(
        token,
        settings.secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "sub", "iss", "ver"]},
    )
