from __future__ import annotations

import json
import re
import secrets
import string
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging import get_logger
from app.models import Link, User
from app.schemas import ALIAS_RE

log = get_logger(__name__)

CODE_RE = ALIAS_RE
_ALPHABET = string.ascii_letters + string.digits
_NEGATIVE = "0"
# Words that must never become short codes because they collide with real routes.
RESERVED = frozenset(
    {"api", "docs", "redoc", "health", "metrics", "admin", "login", "logout", "register", "static", "assets"}
)


def cache_key(code: str) -> str:
    return f"link:{code}"


def generate_code(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


async def cache_forget(redis: Redis, codes: list[str]) -> None:
    try:
        for i in range(0, len(codes), 500):
            await redis.delete(*[cache_key(c) for c in codes[i : i + 500]])
    except RedisError:
        log.warning("cache.invalidate_failed", count=len(codes))


async def create_link(
    session: AsyncSession, redis: Redis, settings: Settings, owner: User, url: str, alias: str | None
) -> Link:
    host = (urlsplit(url).hostname or "").lower()
    if host == settings.public_host:
        raise HTTPException(422, "Links to this service itself are not allowed")
    if alias and alias.lower() in RESERVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "That alias is reserved")

    count = await session.scalar(select(func.count()).select_from(Link).where(Link.owner_id == owner.id))
    if (count or 0) >= settings.max_links_per_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Link quota reached; delete some links first")

    attempts = 1 if alias else 5
    for _ in range(attempts):
        code = alias or generate_code(settings.short_code_length)
        link = Link(code=code, target_url=url, owner_id=owner.id, is_custom=alias is not None)
        session.add(link)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if alias:
                raise HTTPException(status.HTTP_409_CONFLICT, "That alias is already taken") from None
            continue
        try:  # a previous negative-cache entry for this code must not shadow the new link
            await redis.delete(cache_key(code))
        except RedisError:
            pass
        return link
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not allocate a short code, please retry")


async def resolve(
    session: AsyncSession, redis: Redis, settings: Settings, code: str
) -> tuple[tuple[int, str] | None, str]:
    """Look up a code. Returns ((link_id, target_url) | None, outcome) where outcome is hit|miss|not_found."""
    key = cache_key(code)
    try:
        raw = await redis.get(key)
    except RedisError:
        log.warning("cache.read_failed")
        raw = None
    if raw is not None:
        if raw == _NEGATIVE:
            return None, "not_found"
        data = json.loads(raw)
        return (data["id"], data["url"]), "hit"

    row = (await session.execute(select(Link.id, Link.target_url).where(Link.code == code))).first()
    try:
        if row is None:
            await redis.set(key, _NEGATIVE, ex=settings.negative_cache_ttl_seconds)
        else:
            await redis.set(key, json.dumps({"id": row.id, "url": row.target_url}), ex=settings.cache_ttl_seconds)
    except RedisError:
        log.warning("cache.write_failed")
    if row is None:
        return None, "not_found"
    return (row.id, row.target_url), "miss"
