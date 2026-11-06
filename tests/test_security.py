"""Security regression guards: secrets, CORS, auth errors, mount paths, dashboard XSS."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.schemas import PolicyFile

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "dashboard" / "templates"


# --- Secrets ------------------------------------------------------------------------------


def test_settings_repr_and_dump_do_not_contain_passwords():
    s = Settings(_env_file=None, sqlserver_password="Sql-Secret-1", redis_password="Redis-Secret-2")
    for text in (repr(s), str(s.model_dump()), s.model_dump_json()):
        assert "Sql-Secret-1" not in text
        assert "Redis-Secret-2" not in text
    # The properties still build the real connection strings.
    assert "PWD=Sql-Secret-1;" in s.sqlserver_connection_string
    assert ":Redis-Secret-2@" in s.redis_url


# --- CORS ---------------------------------------------------------------------------------


def test_cors_default_allows_no_origins():
    assert Settings(_env_file=None).cors_origins == []


def test_cors_rejects_other_origins_and_never_allows_credentials(client):
    resp = client.options(
        "/api/v1/policies",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert resp.headers.get("access-control-allow-origin") is None
    assert resp.headers.get("access-control-allow-credentials") is None


# --- Auth errors --------------------------------------------------------------------------


async def test_identity_internal_error_text_is_not_returned():
    from app import auth

    auth._token_cache.clear()
    http_client = MagicMock()
    http_client.get = AsyncMock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})  # KeyError inside
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=http_client)))

    with pytest.raises(HTTPException) as exc_info:
        await auth.get_current_user(request, token="tok-123")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


# --- Mount paths --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "\\\\fileserver\\policies\\..\\..\\etc\\passwd",
        "\\\\fileserver\\policies\\2025\\..\\..\\..\\secret.pdf",
        "//fileserver/policies/../outside.pdf",
    ],
)
def test_path_with_parent_segments_never_maps_into_the_mount(path):
    assert PolicyFile.transform_path(path, "/media/polizas") is None
    f = PolicyFile.from_db_row(path, None, local_prefix="/media/polizas")
    assert f.local_path is None
    assert f.available is False
    assert f.unavailable_reason == "excluded"


def test_normal_unc_path_maps_into_the_mount():
    f = PolicyFile.from_db_row(
        "\\\\fileserver\\policies\\2025\\POL-001_TOASEG.pdf", None, local_prefix="/media/polizas"
    )
    assert f.available is True
    assert Path(f.local_path) == Path("/media/polizas/2025/POL-001_TOASEG.pdf")


# --- Dashboard XSS ------------------------------------------------------------------------


def _template_sources():
    return sorted(TEMPLATES.rglob("*.html"))


def test_templates_never_interpolate_into_inline_handlers():
    """No ${...} inside onclick/on* attributes (multi-line attributes included)."""
    pattern = re.compile(r"""\son\w+\s*=\s*(?:"[^"]*\$\{|'[^']*\$\{)""")
    offenders = []
    for path in _template_sources():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(TEMPLATES)}:{line}")
    assert offenders == []


def test_templates_never_put_jinja_values_in_quoted_js_strings():
    """Server values go into <script> through |tojson, never as '{{ x }}'."""
    pattern = re.compile(r"""=\s*['"]\{\{""")
    offenders = []
    for path in _template_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ("const " in line or "let " in line) and pattern.search(line):
                offenders.append(f"{path.relative_to(TEMPLATES)}:{lineno}")
    assert offenders == []


def test_esc_helper_escapes_quotes():
    source = (TEMPLATES / "_js" / "_helpers.js.html").read_text(encoding="utf-8")
    body = source[source.index("function esc(") :]
    body = body[: body.index("\n  }") + 4]
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert entity in body


def test_policy_detail_renders_number_from_url_as_json(client):
    resp = client.get("/dashboard/policies/x'%5C")
    assert resp.status_code == 200
    assert 'const NRO_POL = "x\\u0027\\\\";' in resp.text
