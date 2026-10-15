"""Small Redis-backed throttles used on top of Traefik's per-IP rate limiting.

They fail open (log + allow) if Redis is unavailable so an outage never locks everybody out.
"""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


async def hit(redis: Redis, key: str, window_seconds: int) -> int:
    """Increment a fixed-window counter; returns the new count (0 if Redis is down)."""
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count)
    except RedisError:
        log.warning("ratelimit.redis_unavailable")
        return 0


def _login_keys(email: str, ip: str) -> tuple[str, str]:
    e = _digest(email)
    return f"login:fail:{e}:{_digest(ip)}", f"login:fail:{e}"


async def login_lock_remaining(redis: Redis, settings: Settings, email: str, ip: str) -> int:
    """Seconds the caller must wait, or 0 if login attempts are allowed."""
    pair_key, email_key = _login_keys(email, ip)
    limits = {pair_key: settings.login_max_failures, email_key: settings.login_max_failures * 4}
    try:
        values = await redis.mget(list(limits))
        wait = 0
        for (key, limit), value in zip(limits.items(), values, strict=True):
            if value is not None and int(value) >= limit:
                wait = max(wait, await redis.ttl(key))
        return max(wait, 0)
    except RedisError:
        log.warning("ratelimit.redis_unavailable")
        return 0


async def login_failed(redis: Redis, settings: Settings, email: str, ip: str) -> None:
    pair_key, email_key = _login_keys(email, ip)
    await hit(redis, pair_key, settings.login_lockout_seconds)
    await hit(redis, email_key, settings.login_lockout_seconds)


async def login_succeeded(redis: Redis, email: str, ip: str) -> None:
    try:
        await redis.delete(_login_keys(email, ip)[0])
    except RedisError:
        pass


async def allow_once(redis: Redis, scope: str, subject: str, window_seconds: int) -> bool:
    """True the first time `subject` is seen within the window (used to throttle e-mails)."""
    return await hit(redis, f"once:{scope}:{_digest(subject)}", window_seconds) <= 1
