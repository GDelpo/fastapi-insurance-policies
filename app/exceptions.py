"""Custom exceptions and handlers."""

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from app.logger import get_logger
from app.schemas import ErrorResponse

logger = get_logger(__name__)


# ============================================================================
# Custom Exceptions
# ============================================================================


class ServiceException(Exception):
    """Base service exception."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class EntityNotFoundError(ServiceException):
    """Entity not found."""

    def __init__(self, entity: str, identifier: str | int):
        super().__init__(
            message=f"{entity} '{identifier}' not found",
            status_code=status.HTTP_404_NOT_FOUND,
            details={"entity": entity, "identifier": str(identifier)},
        )


class AuthenticationError(ServiceException):
    """Authentication failed."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            details={"www_authenticate": "Bearer"},
        )


class AuthorizationError(ServiceException):
    """Authorization failed."""

    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message=message, status_code=status.HTTP_403_FORBIDDEN)


class DatabaseError(ServiceException):
    """Database operation failed."""

    def __init__(self, operation: str, message: str):
        super().__init__(
            message=f"Database {operation} failed: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"operation": operation},
        )


class SQLServerConnectionError(ServiceException):
    """SQL Server connection failed."""

    def __init__(self, message: str = "Could not connect to SQL Server"):
        super().__init__(
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            details={"database": "sqlserver"},
        )


class CacheError(ServiceException):
    """Cache operation failed."""

    def __init__(self, operation: str, message: str):
        super().__init__(
            message=f"Cache {operation} failed: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"operation": operation},
        )


class ExternalServiceError(ServiceException):
    """External service call failed (Storage, Identity, etc.)."""

    def __init__(self, service: str, message: str):
        super().__init__(
            message=f"External service '{service}' error: {message}",
            status_code=status.HTTP_502_BAD_GATEWAY,
            details={"service": service},
        )


# ============================================================================
# Exception Handlers
# ============================================================================


async def service_exception_handler(
    request: Request,
    exc: ServiceException,
) -> JSONResponse:
    """Handle ServiceException and subclasses."""
    logger.warning(
        f"Service exception: {exc.message}",
        extra={"extra_fields": {"status_code": exc.status_code, **exc.details}},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            success=False,
            error=type(exc).__name__,
            message=exc.message,
            details=exc.details,
        ).model_dump(),
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError | PydanticValidationError,
) -> JSONResponse:
    """Handle validation errors."""
    errors = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc)}]
    logger.warning(
        "Validation error",
        extra={"extra_fields": {"errors": errors}},
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            success=False,
            error="ValidationError",
            message="Request validation failed",
            details={"errors": errors},
        ).model_dump(),
    )


async def general_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Handle unexpected exceptions."""
    logger.error(
        "Unexpected error: %s",
        exc,
        extra={"extra_fields": {"error_type": type(exc).__name__}},
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            success=False,
            error="InternalServerError",
            message="An unexpected error occurred",
            details={},
        ).model_dump(),
    )
