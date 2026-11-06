"""Dashboard routes — serves the admin UI as HTML pages.

All pages require a valid token stored in localStorage.
The dashboard calls the existing JSON API under the hood.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _DIR / "static"
templates = Jinja2Templates(directory=str(_DIR / "templates"))

router = APIRouter(tags=["Dashboard"], include_in_schema=False)


# ============================================================================
# Static files (self-hosted Tailwind, Lucide, fonts)
# ============================================================================
_MIME = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


@router.get("/static/{path:path}")
async def dashboard_static(path: str):
    """Serve dashboard static assets (JS, CSS, fonts)."""
    file = _STATIC_DIR / path
    if not file.is_file() or not file.resolve().is_relative_to(_STATIC_DIR.resolve()):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Not found"}, status_code=404)
    media_type = _MIME.get(file.suffix, "application/octet-stream")
    return FileResponse(file, media_type=media_type)


def _base(request: Request) -> str:
    """Get the external base path (e.g., '/polizas') from root_path."""
    return (request.scope.get("root_path") or "").rstrip("/")


# ============================================================================
# Shared template context
# ============================================================================

_NAV_ITEMS = [
    {"href_suffix": "/dashboard/", "icon": "bar-chart-3", "label": "Resumen", "key": "overview"},
    {
        "href_suffix": "/dashboard/policies",
        "icon": "scroll-text",
        "label": "Pólizas",
        "key": "policies",
    },
    {
        "href_suffix": "/dashboard/readiness",
        "icon": "check-circle",
        "label": "Disponibilidad",
        "key": "readiness",
    },
    {"href_suffix": "/dashboard/cache", "icon": "database", "label": "Caché", "key": "cache"},
]


def _ctx(request: Request, *, page_title: str, active_key: str, **extra) -> dict:
    """Build the common template context for authenticated pages."""
    base = _base(request)
    nav_items = [
        {**item, "href": base + item["href_suffix"], "active": item["key"] == active_key}
        for item in _NAV_ITEMS
    ]
    return {
        "request": request,
        "base": base,
        "service_name": "Pólizas",
        "service_icon": "scroll-text",
        "nav_items": nav_items,
        "docs_url": settings.docs_url,
        "page_title": page_title,
        **extra,
    }


# ============================================================================
# Pages
# ============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """Overview — stats + health + recent policies."""
    return templates.TemplateResponse(
        "overview.html",
        _ctx(request, page_title="Resumen", active_key="overview"),
    )


@router.get("/policies", response_class=HTMLResponse)
async def dashboard_policies(request: Request):
    """Policies list page."""
    return templates.TemplateResponse(
        "policies.html",
        _ctx(request, page_title="Pólizas", active_key="policies"),
    )


@router.get("/policies/{nro_pol}", response_class=HTMLResponse)
async def dashboard_policy_detail(request: Request, nro_pol: str):
    """Policy detail page."""
    return templates.TemplateResponse(
        "policy_detail.html",
        _ctx(
            request,
            page_title="Póliza",
            active_key="policies",
            nro_pol=nro_pol,
        ),
    )


@router.get("/readiness", response_class=HTMLResponse)
async def dashboard_readiness(request: Request):
    """Readiness check page."""
    return templates.TemplateResponse(
        "readiness.html",
        _ctx(request, page_title="Disponibilidad", active_key="readiness"),
    )


@router.get("/cache", response_class=HTMLResponse)
async def dashboard_cache(request: Request):
    """Cache management page."""
    return templates.TemplateResponse(
        "cache.html",
        _ctx(request, page_title="Caché", active_key="cache"),
    )


@router.get("/login", response_class=HTMLResponse)
async def dashboard_login(request: Request):
    """Login page."""
    base = _base(request)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "base": base,
            "service_name": "Pólizas",
            "service_icon": "scroll-text",
            "identity_base": (
                settings.identity_external_url or settings.identity_service_url
            ).rstrip("/"),
        },
    )
