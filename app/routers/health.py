from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.logging import get_logger
from app.schemas import HealthOut

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)
_TIMEOUT = 2.0


async def _check(name: str, coro) -> str:
    try:
        await asyncio.wait_for(coro, _TIMEOUT)
        return "ok"
    except Exception as exc:  # report only that it failed, never the error text, to the public
        log.warning("health.check_failed", component=name, error=type(exc).__name__)
        return "error"


@router.get("/live", response_model=HealthOut, summary="Liveness (process is up)")
async def live() -> HealthOut:
    return HealthOut(status="ok", version=__version__, checks={})


async def _db_ping(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get(
    "",
    response_model=HealthOut,
    summary="Readiness (PostgreSQL + Redis)",
    responses={503: {"model": HealthOut, "description": "A dependency is unavailable"}},
)
async def health(request: Request):
    database, redis = await asyncio.gather(
        _check("database", _db_ping(request)),
        _check("redis", request.app.state.redis.ping()),
    )
    checks = {"database": database, "redis": redis}
    healthy = all(v == "ok" for v in checks.values())
    body = HealthOut(status="ok" if healthy else "unhealthy", version=__version__, checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)
