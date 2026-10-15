from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from app import __version__
from app.config import get_settings
from app.db import create_engine, create_sessionmaker
from app.logging import configure_logging, get_logger
from app.middleware import ObservabilityMiddleware
from app.routers import admin, auth, health, links, redirect, users
from app.security import hash_password
from app.services import clicks
from app.services.seed import seed_admin

DESCRIPTION = """
URL shortener API.

* **Register**, verify your email and **log in** (`/api/v1/auth/*`). Use the *Authorize* button with your email as the username.
* Create short links with an optional custom alias (`/api/v1/links`).
* Public short codes redirect from `/{code}`.
* Admins can list and delete users under `/api/v1/admin`.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        settings.log_level, settings.log_json, service="url-shortener", environment=settings.environment
    )
    log = get_logger("app")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        sessionmaker = create_sessionmaker(engine)
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3, health_check_interval=30
        )
        app.state.engine, app.state.sessionmaker, app.state.redis = engine, sessionmaker, redis
        app.state.dummy_hash = await hash_password(settings, secrets.token_urlsafe(16))

        await seed_admin(settings, sessionmaker)
        flusher = asyncio.create_task(
            clicks.flush_loop(redis, sessionmaker, settings.click_flush_interval_seconds), name="click-flusher"
        )
        log.info("app.started", version=__version__)
        try:
            yield
        finally:
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
            try:  # don't lose clicks that were buffered when we were told to stop
                await clicks.flush_clicks(redis, sessionmaker)
            except Exception:
                log.exception("clicks.final_flush_failed")
            await redis.aclose()
            await engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        swagger_ui_parameters={"persistAuthorization": True},
    )
    app.add_middleware(ObservabilityMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(links.router)
    app.include_router(admin.router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> PlainTextResponse:
        # Prometheus scrapes the container directly. Anything that came through the reverse proxy
        # carries X-Forwarded-* headers; refuse it so metrics can never leak via a routing mistake.
        if "x-forwarded-for" in request.headers or "x-forwarded-host" in request.headers:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    # Must come last: "/{code}" would otherwise shadow the fixed routes above.
    app.include_router(redirect.router)
    return app
