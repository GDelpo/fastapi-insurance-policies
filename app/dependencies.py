"""FastAPI dependencies for authentication and services.

Follows identidad/mailsender DI patterns:
  - Singletons from app.state (created once in lifespan)
  - Pydantic models for pagination and filter params
  - Factory functions for DI via Depends()
  - Annotated type aliases for clean route signatures
"""

from datetime import date
from typing import Annotated

from fastapi import Depends, Query, Request
from pydantic import BaseModel, Field

from app.auth import TokenData, admin_role_required, service_role_required
from app.service import PolicyService

# =========================================================================
# Service dependency (singleton from app.state)
# =========================================================================


def get_policy_service(request: Request) -> PolicyService:
    """Get policy service from app state."""
    return request.app.state.policy_service


# =========================================================================
# Pagination params — matches identidad/mailsender pattern
# =========================================================================


class PaginationParams(BaseModel):
    """Pagination parameters injected via Depends()."""

    limit: int = Field(default=20, ge=1, le=5000)
    skip: int = Field(default=0, ge=0)


def get_pagination_params(
    limit: Annotated[int, Query(ge=1, le=5000, description="Maximum items to return")] = 20,
    skip: Annotated[int, Query(ge=0, description="Items to skip")] = 0,
) -> PaginationParams:
    """Build pagination params from query string."""
    return PaginationParams(limit=limit, skip=skip)


# =========================================================================
# Policy filter params
# =========================================================================


class PolicyFilterParams(BaseModel):
    """Filter parameters for policy listing."""

    emission_date: date | None = None
    creation_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    has_files: bool = False
    has_unavailable: bool = False
    sort_order: str = "desc"
    sort_by: str = "emission_date"


def get_policy_filters(
    emission_date: Annotated[date | None, Query(description="Filter by emission date")] = None,
    creation_date: Annotated[date | None, Query(description="Filter by PDF creation date")] = None,
    start_date: Annotated[date | None, Query(description="Emission date range — start")] = None,
    end_date: Annotated[date | None, Query(description="Emission date range — end")] = None,
    has_files: Annotated[bool, Query(description="Only policies with at least one file")] = False,
    has_unavailable: Annotated[
        bool, Query(description="Only policies with at least one unavailable file")
    ] = False,
    sort_order: Annotated[
        str,
        Query(
            description="Sort direction: 'desc' (newest first) or 'asc' (oldest first)",
            pattern="^(asc|desc)$",
        ),
    ] = "desc",
    sort_by: Annotated[
        str,
        Query(
            description="Sort field: 'emission_date' or 'nro_pol'",
            pattern="^(emission_date|nro_pol)$",
        ),
    ] = "emission_date",
) -> PolicyFilterParams:
    """Build policy filter params from query string."""
    return PolicyFilterParams(
        emission_date=emission_date,
        creation_date=creation_date,
        start_date=start_date,
        end_date=end_date,
        has_files=has_files,
        has_unavailable=has_unavailable,
        sort_order=sort_order,
        sort_by=sort_by,
    )


# =========================================================================
# Type aliases for cleaner endpoint signatures
# =========================================================================

ServiceUser = Annotated[TokenData, Depends(service_role_required)]
AdminUser = Annotated[TokenData, Depends(admin_role_required)]
PolicyServiceDep = Annotated[PolicyService, Depends(get_policy_service)]
Pagination = Annotated[PaginationParams, Depends(get_pagination_params)]
PolicyFilters = Annotated[PolicyFilterParams, Depends(get_policy_filters)]
