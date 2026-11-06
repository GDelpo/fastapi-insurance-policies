"""API routes for the Policy Service.

Thin controller layer — delegates all business logic to PolicyService.
Follows identidad/mailsender patterns:
  - Pydantic DI params (Pagination, PolicyFilters) via Depends()
  - PaginatedResponse[T] for list endpoints
  - No manual exception catching — global handlers manage ServiceException
  - response_model, summary, description on every endpoint

Security levels:
    ServiceUser  — inter-service calls (sender, etc.)
    AdminUser    — management/monitoring (producers, insureds, cache)
"""

from datetime import date

from fastapi import APIRouter, Query

from app.dependencies import (
    AdminUser,
    Pagination,
    PolicyFilters,
    PolicyServiceDep,
    ServiceUser,
)
from app.logger import get_logger
from app.schemas import (
    CacheWarmupResponse,
    InsuredResponse,
    MarkSentRequest,
    MarkSentResponse,
    PaginatedResponse,
    PaginationMeta,
    Policy,
    ProducerResponse,
    ReadinessResponse,
    StorageMetadataResponse,
    StorageMetadataUpdate,
)

logger = get_logger(__name__)

router = APIRouter()


# =========================================================================
# Policy endpoints
# =========================================================================


@router.get(
    "/policies",
    response_model=PaginatedResponse[Policy],
    tags=["Policies"],
    summary="List policies",
    description=(
        "List policies with optional filters. All filters are composable.\n\n"
        "**Filters:**\n"
        "- `emission_date` — policies emitted on that date (cache-first)\n"
        "- `creation_date` — policies whose PDF was created on that date\n"
        "- `start_date` + `end_date` — emission date range\n"
        "- `has_files` — only policies with at least one available file\n"
        "- `order` — 'desc' (newest first) or 'asc' (oldest first)\n\n"
        "Storage metadata (in_storage, storage_id, storage_link) is automatically "
        "enriched from cached metadata written by the dispatcher service."
    ),
)
async def list_policies(
    service: PolicyServiceDep,
    current_user: ServiceUser,
    filters: PolicyFilters,
    pagination: Pagination,
) -> PaginatedResponse[Policy]:
    """List policies with filtering and pagination."""
    policies, total = await service.list_policies(filters=filters, pagination=pagination)
    return PaginatedResponse(
        data=policies,
        meta=PaginationMeta.from_params(
            pagination.limit,
            pagination.skip,
            total,
        ),
    )


@router.get(
    "/policies/readiness",
    response_model=ReadinessResponse,
    tags=["Readiness"],
    summary="Check readiness for a date",
    description=(
        "Check if all policies for a date have their PDF files ready.\n\n"
        "Returns total/complete/missing counts and a ready flag.\n"
        "Used by the sender service before starting an email batch."
    ),
)
async def check_readiness(
    service: PolicyServiceDep,
    current_user: ServiceUser,
    emission_date: date = Query(..., description="Emission date to check"),
    include_missing: bool = Query(True, description="Include list of missing policies"),
    refresh: bool = Query(False, description="Force refresh from DB"),
) -> ReadinessResponse:
    """Check if all policies for a date have their PDF files ready."""
    return await service.check_readiness(
        emission_date,
        include_missing=include_missing,
        refresh=refresh,
    )


@router.get(
    "/policies/{nro_pol}",
    response_model=Policy,
    tags=["Policies"],
    summary="Get policy by number",
    description=(
        "Get detailed policy with full hierarchy: endorsements, files, insured, producer.\n"
        "Storage metadata is automatically enriched from cached data."
    ),
)
async def get_policy(
    nro_pol: str,
    service: PolicyServiceDep,
    current_user: ServiceUser,
) -> Policy:
    """Get policy by number with full hierarchy."""
    return await service.get_policy(nro_pol)


# =========================================================================
# Storage metadata endpoints (Reverse Flow — written by dispatcher)
# =========================================================================


@router.patch(
    "/policies/{policy_id}/files/{file_id}/storage",
    response_model=StorageMetadataResponse,
    tags=["Storage Metadata"],
    summary="Update storage metadata for a policy file",
    description=(
        "Save cloud storage metadata (storage_id, storage_link) for a specific policy file.\n\n"
        "Called by the dispatcher service after uploading a file to storage.\n"
        "Metadata is stored in Redis and served transparently on policy queries."
    ),
)
async def update_storage_metadata(
    policy_id: str,
    file_id: str,
    metadata: StorageMetadataUpdate,
    service: PolicyServiceDep,
    current_user: ServiceUser,
) -> StorageMetadataResponse:
    """Save storage metadata for a policy file."""
    return await service.save_storage_metadata(policy_id, file_id, metadata)


@router.post(
    "/policies/mark-sent",
    response_model=MarkSentResponse,
    tags=["Storage Metadata"],
    summary="Mark policies as sent in timpresion_pdf",
    description=(
        "Set sn_envio = 1 and fec_envio = sent_date in timpresion_pdf for all\n"
        "PDF rows belonging to the given policy numbers.\n\n"
        "Called by the dispatcher after a successful email send.\n"
        "Only rows not yet marked are updated (idempotent)."
    ),
)
async def mark_policies_sent(
    body: MarkSentRequest,
    service: PolicyServiceDep,
    current_user: ServiceUser,
) -> MarkSentResponse:
    """Mark policies as sent in SQL Server."""
    updated = await service.mark_sent(body.nro_pol_list, body.sent_date)
    return MarkSentResponse(updated_rows=updated, policy_count=len(body.nro_pol_list))


@router.delete(
    "/policies/{policy_id}/files/{file_id}/storage",
    tags=["Storage Metadata"],
    summary="Delete storage metadata for a policy file",
    description="Remove cached storage metadata for a specific policy file.",
)
async def delete_storage_metadata(
    policy_id: str,
    file_id: str,
    service: PolicyServiceDep,
    current_user: ServiceUser,
) -> dict:
    """Delete storage metadata for a policy file."""
    deleted = await service.delete_storage_metadata(policy_id, file_id)
    if deleted:
        return {"message": f"Storage metadata deleted for {policy_id}/{file_id}"}
    return {"message": f"No storage metadata found for {policy_id}/{file_id}"}


# =========================================================================
# Producer endpoints
# =========================================================================


@router.get(
    "/producers",
    response_model=PaginatedResponse[ProducerResponse],
    tags=["Producers"],
    summary="List producers",
    description="List all producers with optional name or email search. Sorted by name.",
)
async def list_producers(
    service: PolicyServiceDep,
    current_user: AdminUser,
    pagination: Pagination,
    search: str | None = Query(None, description="Filter by name or email (partial match)"),
    sort_order: str = Query(
        "asc", pattern="^(asc|desc)$", description="Sort direction: 'asc' (A→Z) or 'desc' (Z→A)"
    ),
) -> PaginatedResponse[ProducerResponse]:
    """List producers with search and pagination."""
    producers = await service.get_producers(
        limit=pagination.limit,
        offset=pagination.skip,
        search=search,
        order_dir=sort_order,
    )
    return PaginatedResponse(
        data=producers,
        meta=PaginationMeta.from_params(
            pagination.limit,
            pagination.skip,
            len(producers),
        ),
    )


@router.get(
    "/producers/{code}",
    response_model=ProducerResponse,
    tags=["Producers"],
    summary="Get producer by code",
    description="Get producer information by agent code.",
)
async def get_producer(
    code: str,
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> ProducerResponse:
    """Get producer by code."""
    return await service.get_producer(code)


# =========================================================================
# Insured endpoints
# =========================================================================


@router.get(
    "/insureds",
    response_model=PaginatedResponse[InsuredResponse],
    tags=["Insureds"],
    summary="List insureds",
    description="List all insureds with optional name or email search. Sorted by name.",
)
async def list_insureds(
    service: PolicyServiceDep,
    current_user: AdminUser,
    pagination: Pagination,
    search: str | None = Query(None, description="Filter by name or email (partial match)"),
    sort_order: str = Query(
        "asc", pattern="^(asc|desc)$", description="Sort direction: 'asc' (A→Z) or 'desc' (Z→A)"
    ),
) -> PaginatedResponse[InsuredResponse]:
    """List insureds with search and pagination."""
    insureds = await service.get_insureds(
        limit=pagination.limit,
        offset=pagination.skip,
        search=search,
        order_dir=sort_order,
    )
    return PaginatedResponse(
        data=insureds,
        meta=PaginationMeta.from_params(
            pagination.limit,
            pagination.skip,
            len(insureds),
        ),
    )


@router.get(
    "/insureds/{code}",
    response_model=InsuredResponse,
    tags=["Insureds"],
    summary="Get insured by code",
    description="Get insured information by insured code (cod_aseg).",
)
async def get_insured(
    code: str,
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> InsuredResponse:
    """Get insured by code."""
    return await service.get_insured(code)


# =========================================================================
# Cache management (admin only)
# =========================================================================


@router.get(
    "/cache/stats",
    tags=["Cache"],
    summary="Get cache statistics",
    description="Returns total policies cached, unique dates, producers indexed, and last sync timestamp.",
)
async def get_cache_stats(
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> dict:
    """Get Redis cache statistics."""
    return await service.get_cache_stats()


@router.get(
    "/cache/diagnostics",
    tags=["Cache"],
    summary="Get file sync diagnostics",
    description=(
        "Scans all cached policies and returns a diagnostic report.\n\n"
        "**Pending files**: available on disk but not yet uploaded to storage, "
        "bucketed by age (last 7d / 8-30d / 31-90d / >90d).\n\n"
        "**Unavailable files**: marked as 'ARCHIVO NO DISPONIBLE' in the database.\n\n"
        "This operation scans all Redis policy keys — may take several seconds on large caches."
    ),
)
async def get_cache_diagnostics(
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> dict:
    """Get detailed file sync diagnostic report."""
    return await service.get_cache_diagnostics()


@router.post(
    "/cache/refresh",
    tags=["Cache"],
    summary="Refresh cache with recent policies",
    description=(
        "Update cache with policies from the last N days.\n"
        "Does NOT clear existing cache — just updates/adds recent policies.\n"
        "Use DELETE /cache/clear first if you want a fresh start."
    ),
)
async def refresh_cache(
    service: PolicyServiceDep,
    current_user: AdminUser,
    days: int = Query(30, ge=1, le=90, description="Number of days to refresh"),
) -> dict:
    """Refresh cache with recent policies."""
    stats = await service.refresh_cache(days=days)
    return {"message": f"Cache refreshed with last {days} days", "stats": stats}


@router.post(
    "/cache/warmup",
    response_model=CacheWarmupResponse,
    tags=["Cache"],
    summary="Warm up cache",
    description="Pre-load policies from the last N days into Redis cache.",
)
async def warmup_cache(
    service: PolicyServiceDep,
    current_user: AdminUser,
    days: int = Query(7, ge=1, le=90, description="Number of days to preload"),
) -> CacheWarmupResponse:
    """Warm up cache with recent policies."""
    return await service.warmup_cache(days)


@router.delete(
    "/cache/policies/{nro_pol}",
    tags=["Cache"],
    summary="Invalidate policy cache",
    description="Invalidate cache for a specific policy and reload from DB.",
)
async def invalidate_policy_cache(
    nro_pol: str,
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> dict:
    """Invalidate cache for a specific policy."""
    await service.invalidate_policy_cache(nro_pol)
    return {"message": f"Cache invalidated for policy {nro_pol}"}


@router.delete(
    "/cache/date/{target_date}",
    tags=["Cache"],
    summary="Invalidate date cache",
    description="Invalidate cache for a specific date.",
)
async def invalidate_date_cache(
    target_date: date,
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> dict:
    """Invalidate cache for a specific date."""
    await service.invalidate_date_cache(target_date)
    return {"message": f"Cache invalidated for date {target_date}"}


@router.delete(
    "/cache/clear",
    tags=["Cache"],
    summary="Clear all policy cache",
    description="Clear ALL policy data from Redis cache.",
)
async def clear_cache(
    service: PolicyServiceDep,
    current_user: AdminUser,
) -> dict:
    """Clear ALL policy data from Redis cache."""
    count = await service.clear_cache()
    return {"message": f"Cache cleared: {count} keys deleted"}
