"""Authentication via Identity Service.

This service delegates authentication to the Identity Service.
It receives a Bearer token and validates it by calling the Identity Service's /me endpoint.
Token validation results are cached in memory to avoid hitting identidad on every request
(crítico durante un bulk sync: cientos de miles de PATCHes con el mismo service token).
"""

import time
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

# Cache de validaciones de token — evita llamar a identidad en cada request.
# Key: token string, Value: (TokenData, expiry_timestamp)
_token_cache: dict[str, tuple["TokenData", float]] = {}
_TOKEN_CACHE_TTL = 300  # 5 minutos — menos que el TTL real del JWT
_TOKEN_CACHE_MAX = 500  # evita crecimiento ilimitado en rotación de tokens


# OAuth2 scheme - tokenUrl points to identity service login (for Swagger UI)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=settings.identity_login_url)


class TokenData(BaseModel):
    """Token data from identity service."""

    id: UUID
    user_name: str
    mail: str | None = None
    role: str


async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> TokenData:
    """Validate token with identity service and get user data.

    Args:
        request: FastAPI request (for shared HTTP client)
        token: Bearer token from Authorization header

    Returns:
        TokenData with user information

    Raises:
        HTTPException: If token validation fails
    """
    # Cache hit — evita roundtrip a identidad para el mismo token
    cached = _token_cache.get(token)
    if cached:
        token_data, expiry = cached
        if time.monotonic() < expiry:
            return token_data
        del _token_cache[token]

    client = request.app.state.http_client
    try:
        response = await client.get(
            settings.identity_me_url,
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code == 200:
            data = response.json()
            token_data = TokenData(
                id=UUID(data["id"]),
                user_name=data["userName"],
                mail=data.get("mail"),
                role=data["role"],
            )
            if len(_token_cache) >= _TOKEN_CACHE_MAX:
                _token_cache.clear()
            _token_cache[token] = (token_data, time.monotonic() + _TOKEN_CACHE_TTL)
            return token_data

        # Token invalid or expired
        error_msg = response.json().get("detail", "Token validation failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_msg,
            headers={"WWW-Authenticate": "Bearer"},
        )

    except httpx.ConnectTimeout:
        logger.error("Identity service connection timeout")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable (connection timeout)",
        ) from None
    except httpx.ReadTimeout:
        logger.error("Identity service read timeout")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable (read timeout)",
        ) from None
    except httpx.ConnectError:
        logger.error("Cannot connect to identity service")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        # Internal error text (parse errors, KeyErrors, URLs) stays in the log only.
        logger.error("Identity service error (%s): %s", type(e).__name__, e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def service_role_required(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Require service or admin role (inter-service + dashboard calls)."""
    if current_user.role not in ["admin", "service"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service or admin role required",
        )
    return current_user


async def admin_role_required(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Require admin or service role."""
    if current_user.role not in ["admin", "service"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user
