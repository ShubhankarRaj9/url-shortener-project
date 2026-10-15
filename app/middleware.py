"""Pure-ASGI middleware: request ids, structured access logs, Prometheus HTTP metrics."""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import get_logger
from app.metrics import HTTP_IN_PROGRESS, HTTP_LATENCY, HTTP_REQUESTS

log = get_logger("app.access")
_RID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = ("/health", "/metrics")


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _RID_RE.fullmatch(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        started = False
        start = time.perf_counter()
        HTTP_IN_PROGRESS.inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers.setdefault("X-Content-Type-Options", "nosniff")
                if scope["path"].startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("http.unhandled_exception")
            if started:
                raise
            response = JSONResponse(
                {"detail": "Internal server error", "request_id": request_id}, status_code=500
            )
            await response(scope, receive, send_wrapper)
        finally:
            HTTP_IN_PROGRESS.dec()
            duration = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope["method"]
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_LATENCY.labels(method, route).observe(duration)

            client = scope.get("client")
            path = scope["path"]
            level = "error" if status >= 500 else "debug" if path.startswith(_QUIET_PATHS) else "info"
            getattr(log, level)(
                "http.request",
                method=method,
                path=path,
                route=route,
                status=status,
                duration_ms=round(duration * 1000, 2),
                client_ip=client[0] if client else None,
                user_agent=Headers(scope=scope).get("user-agent", "")[:200],
            )
            structlog.contextvars.clear_contextvars()
