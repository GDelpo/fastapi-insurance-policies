"""Request/Response logging and monitoring middleware."""

import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logger import get_logger

logger = get_logger(__name__)


class ProxyHeadersMiddleware:
    """Middleware to handle forwarded prefix headers from reverse proxies.

    This middleware is INFRASTRUCTURE-AGNOSTIC. It reads common headers used by
    different reverse proxies to communicate the original path prefix, and
    configures the ASGI scope's root_path accordingly.

    Supported headers (checked in order of priority):
        - X-Forwarded-Prefix: Common reverse-proxy header, custom configs
        - X-Forwarded-Path: Alternative naming
        - X-Script-Name: WSGI/CGI convention (used by some setups)
    """

    PREFIX_HEADERS = [
        b"x-forwarded-prefix",
        b"x-forwarded-path",
        b"x-script-name",
    ]

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))

            forwarded_prefix = ""
            for header_name in self.PREFIX_HEADERS:
                if header_name in headers:
                    forwarded_prefix = headers[header_name].decode("utf-8")
                    break

            if forwarded_prefix:
                prefix = forwarded_prefix.strip().rstrip("/")
                if prefix and not prefix.startswith("/"):
                    prefix = "/" + prefix
                scope["root_path"] = prefix

        await self.app(scope, receive, send)


class RequestLoggingMiddleware:
    """Pure ASGI middleware for request/response logging.

    Logs incoming requests and outgoing responses with timing,
    status-based log levels, and request ID tracing.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract request info from ASGI scope
        method = scope.get("method", "?")
        path = scope.get("path", "/")
        query_string = (scope.get("query_string") or b"").decode("utf-8", errors="replace")
        headers = dict(scope.get("headers", []))
        client = scope.get("client")
        client_host = client[0] if client else "unknown"
        user_agent = headers.get(b"user-agent", b"unknown").decode("utf-8", errors="replace")

        # Get or generate request ID
        request_id: str | None = None
        for header_name in (b"x-request-id", b"x-correlation-id"):
            if header_name in headers:
                request_id = headers[header_name].decode("utf-8")
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        # Store in scope state so downstream Request.state.request_id works
        scope.setdefault("state", {})["request_id"] = request_id

        start_time = time.time()

        logger.info(
            "📥 %s %s",
            method,
            path,
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "query_string": query_string,
                    "client_host": client_host,
                    "user_agent": user_agent,
                }
            },
        )

        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                # Inject X-Request-ID into response headers
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": raw_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                "❌ %s %s - Exception (%s)",
                method,
                path,
                type(exc).__name__,
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "duration_ms": round(duration_ms, 2),
                        "error": str(exc),
                    }
                },
                exc_info=True,
            )
            raise

        duration_ms = (time.time() - start_time) * 1000

        if status_code < 300:
            emoji, log_level = "✅", logging.INFO
        elif status_code < 400:
            emoji, log_level = "➡️", logging.INFO
        elif status_code < 500:
            emoji, log_level = "⚠️", logging.WARNING
        else:
            emoji, log_level = "❌", logging.ERROR

        logger.log(
            log_level,
            "%s %s %s - %s (%.2fms)",
            emoji,
            method,
            path,
            status_code,
            duration_ms,
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "client_host": client_host,
                }
            },
        )
