"""Policy Service - Main application entry point.

This application is infrastructure-agnostic. It runs the same way:
- Locally on port 8082
- Behind a reverse proxy (Caddy, Nginx, ...) with any path prefix
"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from app.api import router
from app.cache import CacheManager
from app.config import settings
from app.dashboard.routes import router as dashboard_router
from app.exceptions import (
    ServiceException,
    general_exception_handler,
    service_exception_handler,
    validation_exception_handler,
)
from app.logger import configure_logging, get_logger
from app.middleware import ProxyHeadersMiddleware, RequestLoggingMiddleware
from app.repository import PolicyRepository
from app.schemas import DependencyHealth, HealthResponse, ServiceStatus
from app.service import PolicyService
from app.sql_client import SQLServerClient

# Configure logging before anything else
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Application lifespan handler.

    1. On startup: Initialize SQL, Redis, warmup cache (last 2 years by default)
    2. On shutdown: Cleanup connections

    Configure CACHE_WARMUP_DAYS env var to change warmup period (0 = disabled).
    """
    logger.info(
        "🚀 Starting Policy Service",
        extra={
            "extra_fields": {
                "version": settings.project_version,
                "environment": settings.environment,
                "debug": settings.debug,
            }
        },
    )

    # Create singletons
    sql_client = SQLServerClient()
    sql_client.test_connection()

    cache = CacheManager(settings.redis_url)
    await cache.connect()

    repo = PolicyRepository(sql_client)
    service = PolicyService(repo, cache)
    await service.initialize()

    # Store in app.state for dependency injection
    app_instance.state.sql_client = sql_client
    app_instance.state.cache_manager = cache
    app_instance.state.policy_service = service

    # Create shared HTTP client for auth
    app_instance.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )

    yield

    # Close shared HTTP client
    await app_instance.state.http_client.aclose()

    # Cleanup
    logger.info("🛑 Shutting down Policy Service")

    # Shutdown service
    await service.shutdown()

    # Disconnect Redis
    await cache.disconnect()


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title=settings.project_name,
    version=settings.project_version,
    description=settings.project_description,
    # No root_path hardcoded - ProxyHeadersMiddleware reads X-Forwarded-Prefix
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    debug=settings.debug,
    lifespan=lifespan,
)

# =============================================================================
# Middleware (order matters - first added = last executed, outermost first)
# =============================================================================

# Proxy headers middleware (outermost - handles X-Forwarded-Prefix for root_path)
app.add_middleware(ProxyHeadersMiddleware)

# Logging middleware
app.add_middleware(RequestLoggingMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # INTENTIONAL: the bearer token travels in the Authorization header (localStorage),
    # never in cookies, so cross-origin requests have no reason to carry credentials.
    # Credentials + a "*" origin list would let any site make authenticated calls.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# =============================================================================
# Exception Handlers
# =============================================================================

app.add_exception_handler(ServiceException, service_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(PydanticValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# =============================================================================
# Routers
# =============================================================================

app.include_router(router, prefix=settings.api_prefix)
app.include_router(dashboard_router, prefix="/dashboard")


# =============================================================================
# Health Check (at root level, not under api_prefix)
# =============================================================================


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(request: Request) -> HealthResponse:
    """Health check with per-dependency status (matches identidad/mailsender)."""
    import asyncio
    import time as _time

    dependencies: dict[str, DependencyHealth] = {}

    # ---- SQL Server (critical) ----
    # IMPORTANT: test_connection() is synchronous (pyodbc/FreeTDS). Running it
    # directly would block the asyncio event loop. asyncio.to_thread() offloads
    # the blocking call to a thread-pool worker, keeping the loop responsive
    # even when many concurrent requests are in flight.
    t0 = _time.perf_counter()
    try:
        db_ok = await asyncio.to_thread(request.app.state.sql_client.test_connection)
    except Exception:
        db_ok = False
    db_ms = round((_time.perf_counter() - t0) * 1000, 2)
    dependencies["sqlserver"] = DependencyHealth(
        name="sqlserver",
        status=ServiceStatus.HEALTHY if db_ok else ServiceStatus.UNHEALTHY,
        required=True,
        latency_ms=db_ms,
    )

    # ---- Redis (critical) ----
    # IMPORTANTE: no llamar get_cache_stats() aquí — hace SCAN sobre storage:meta:*
    # que puede tener cientos de miles de claves y bloquea el health check bajo carga.
    cache = request.app.state.cache_manager
    redis_ok = False
    redis_details: dict = {}
    try:
        await cache.redis.ping()
        redis_ok = True
        policy_count = await cache.get_all_count()
        redis_details = {"policies_indexed": policy_count}
    except Exception:
        redis_ok = False
    dependencies["redis"] = DependencyHealth(
        name="redis",
        status=ServiceStatus.HEALTHY if redis_ok else ServiceStatus.UNHEALTHY,
        required=True,
        details=redis_details,
    )

    # ---- Overall ----
    critical = [d for d in dependencies.values() if d.required]
    if all(d.status == ServiceStatus.HEALTHY for d in critical):
        overall = ServiceStatus.HEALTHY
    elif any(d.status == ServiceStatus.UNHEALTHY for d in critical):
        overall = ServiceStatus.UNHEALTHY
    else:
        overall = ServiceStatus.DEGRADED

    resp = HealthResponse(
        status=overall,
        version=settings.project_version,
        environment=settings.environment,
        dependencies=dependencies,
    )

    # Return 503 when critical deps are down (identidad pattern)
    if overall == ServiceStatus.UNHEALTHY:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=resp.model_dump(mode="json", by_alias=True),
        )
    return resp


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8082,
        reload=settings.debug,
    )
