"""Exact click counting.

Recording (hot path, one O(1) Redis command): every redirect appends an entry to a Redis stream.

Flushing (background, one replica at a time via a Redis lock): in ONE PostgreSQL transaction the flusher
  1. reads the watermark (`click_flush_state.last_id`) = the last stream entry already counted,
  2. reads the stream entries after it,
  3. adds them to `links.click_count`, and
  4. moves the watermark forward.
Only after that commit are the entries trimmed from Redis. Consequences:
  * crash before the commit  -> nothing was counted, the same entries are read again;
  * crash after the commit   -> the watermark already covers them, so they are skipped (and trimmed later);
  * so every click is counted exactly once, however often the flusher is interrupted or retried.

If Redis is unavailable when a click happens, the click is written straight to PostgreSQL instead (the
redirect already needs the database in that situation), so it is not lost either. A click is only lost if
Redis AND PostgreSQL are both down at that instant, or if Redis itself loses acknowledged data (AOF fsyncs
every second, so a hard crash of the Redis host can lose up to ~1 s of buffered clicks).
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from datetime import datetime, timezone

from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import DateTime, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logging import get_logger
from app.metrics import CLICK_FLUSH_FAILURES, CLICKS_FALLBACK, CLICKS_FLUSHED, CLICKS_LOST, CLICKS_PENDING
from app.models import ClickFlushState, Link

log = get_logger(__name__)

STREAM = "clicks:stream"
STATE_NAME = "clicks"
LOCK = "clicks:flush-lock"
BATCH_SIZE = 5000
MAX_BATCHES_PER_RUN = 50

# Keys used by the previous (hash based) implementation; drained once on upgrade.
_LEGACY_KEYS = ("clicks:processing", "clicks:pending")

_links = Link.__table__
_ts = bindparam("ts", type_=DateTime(timezone=True))
_APPLY = (
    update(_links)
    .where(_links.c.id == bindparam("link_id"))
    .values(
        click_count=_links.c.click_count + bindparam("n"),
        # the time of the latest click, not the time of the flush
        last_clicked_at=func.greatest(func.coalesce(_links.c.last_clicked_at, _ts), _ts),
    )
)


def _next_id(stream_id: str) -> str:
    ms, seq = stream_id.split("-")
    return f"{ms}-{int(seq) + 1}"


def _ts_of(stream_id: str) -> datetime:
    return datetime.fromtimestamp(int(stream_id.split("-")[0]) / 1000, tz=timezone.utc)


async def record_click(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession], link_id: int) -> None:
    try:
        await redis.xadd(STREAM, {"l": link_id})
        return
    except RedisError:
        log.warning("clicks.redis_unavailable_using_database", link_id=link_id)
    CLICKS_FALLBACK.inc()
    try:
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(_links)
                .where(_links.c.id == link_id)
                .values(click_count=_links.c.click_count + 1, last_clicked_at=func.now())
            )
    except Exception:
        CLICKS_LOST.inc()
        log.exception("clicks.lost", link_id=link_id)


async def _flush_batch(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession], batch: int) -> int:
    async with sessionmaker() as session, session.begin():
        await session.execute(
            pg_insert(ClickFlushState).values(name=STATE_NAME, last_id="0-0").on_conflict_do_nothing()
        )
        last_id = await session.scalar(
            select(ClickFlushState.last_id).where(ClickFlushState.name == STATE_NAME).with_for_update()
        )
        entries = await redis.xrange(STREAM, min=f"({last_id}", max="+", count=batch)
        if entries:
            per_link: dict[int, list] = defaultdict(lambda: [0, None])
            for stream_id, fields in entries:
                agg = per_link[int(fields["l"])]
                agg[0] += 1
                agg[1] = _ts_of(stream_id)  # entries are ordered, so the last one wins
            await session.execute(
                _APPLY, [{"link_id": lid, "n": n, "ts": ts} for lid, (n, ts) in per_link.items()]
            )
            last_id = entries[-1][0]
            await session.execute(
                update(ClickFlushState).where(ClickFlushState.name == STATE_NAME).values(last_id=last_id)
            )
    # ---- committed: everything up to last_id is in PostgreSQL. Trimming is only housekeeping and is
    # idempotent, so a failure here is harmless (the next run trims again and never re-counts).
    if last_id != "0-0":
        try:
            await redis.xtrim(STREAM, minid=_next_id(last_id))
        except RedisError:
            log.warning("clicks.trim_failed")
    return len(entries)


async def _drain_legacy(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    for key in _LEGACY_KEYS:
        data = await redis.hgetall(key)
        if data:
            now = datetime.now(timezone.utc)
            rows = [{"link_id": int(k), "n": int(v), "ts": now} for k, v in data.items()]
            async with sessionmaker() as session, session.begin():
                await session.execute(_APPLY, rows)
            log.info("clicks.legacy_drained", key=key, clicks=sum(r["n"] for r in rows))
        if data is not None:
            await redis.delete(key)


async def flush_clicks(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    batch: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES_PER_RUN,
) -> int:
    """Count pending clicks in PostgreSQL. Returns how many were flushed."""
    lock = redis.lock(LOCK, timeout=60, blocking=False)
    if not await lock.acquire():
        return 0  # another replica is flushing
    total = 0
    try:
        await _drain_legacy(redis, sessionmaker)
        for _ in range(max_batches):
            flushed = await _flush_batch(redis, sessionmaker, batch)
            total += flushed
            CLICKS_FLUSHED.inc(flushed)
            if flushed < batch:
                break
        if total:
            log.info("clicks.flushed", clicks=total)
        with contextlib.suppress(RedisError):
            CLICKS_PENDING.set(await redis.xlen(STREAM))
        return total
    finally:
        with contextlib.suppress(LockError, RedisError):
            await lock.release()


async def flush_loop(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession], interval: float) -> None:
    import asyncio

    while True:
        await asyncio.sleep(interval)
        try:
            await flush_clicks(redis, sessionmaker)
        except asyncio.CancelledError:
            raise
        except Exception:
            CLICK_FLUSH_FAILURES.inc()
            log.exception("clicks.flush_failed")
