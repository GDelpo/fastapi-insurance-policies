"""Service layer for policy operations with Redis caching.

Architecture:
    Polizas = Central Data Service
    - SQL Server: Source of truth (read-only)
    - Redis: Cache-aside with background sync
    - Storage metadata: Stored in Redis, written by dispatcher via PATCH

Cache Strategy:
    1. Startup: Connect Redis, warmup last N days
    2. Queries: Cache-first (Redis → DB → cache result)
    3. Sync: Background loop syncs recent days every N minutes

Storage Metadata (Reverse Flow):
    The dispatcher service uploads files to cloud storage and sends metadata
    back via PATCH /policies/{id}/files/{file_id}/storage.
    Metadata is stored in Redis and enriched transparently on every query.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from app.cache import CacheManager
from app.config import settings
from app.exceptions import ServiceException
from app.logger import get_logger
from app.repository import PolicyRepository
from app.schemas import (
    CacheWarmupResponse,
    InsuredResponse,
    MissingPolicyItem,
    Policy,
    ProducerResponse,
    ReadinessResponse,
    StorageMetadataResponse,
    StorageMetadataUpdate,
)

if TYPE_CHECKING:
    from app.dependencies import PaginationParams, PolicyFilterParams

logger = get_logger(__name__)


class PolicyService:
    """Service for policy operations with Redis cache.

    Uses cache-aside pattern optimized for large datasets:
    - No full load (2M+ records would be too slow)
    - Cache populated on-demand as queries come in
    - Optional warmup for recent days
    - Background sync only for recent dates
    """

    def __init__(
        self,
        repo: PolicyRepository,
        cache: CacheManager,
        sync_interval_minutes: int | None = None,
    ):
        """Initialize service.

        Args:
            repo: SQL Server repository
            cache: Redis cache manager
            sync_interval_minutes: How often to sync recent days (uses config if None)
        """
        self.repo = repo
        self.cache = cache

        # Use config value if not explicitly provided
        interval = sync_interval_minutes or settings.cache_sync_interval_minutes
        self._sync_interval = interval * 60
        self._sync_task: asyncio.Task | None = None
        self._initialized = False

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> dict:
        """Initialize service: connect to Redis, warmup cache, start background sync.

        Warmup loads the last N days (configured by cache_warmup_days).
        Default: 730 days (2 years).

        Returns:
            Cache statistics after initialization
        """
        logger.info("🔄 Initializing PolicyService...")
        start_time = time.time()

        # Connect to Redis
        if not self.cache.is_connected:
            await self.cache.connect()

        if not self.cache.is_connected:
            logger.warning("⚠️ Redis not available, will operate without cache")
            self._initialized = True
            return {"error": "Redis not available"}

        # Warmup cache — load from Jan 1 of (current_year - 2) to cover full calendar years
        warmup_days = settings.cache_warmup_days
        if warmup_days > 0:
            today = date.today()
            calendar_start = date(today.year - 2, 1, 1)
            effective_days = (today - calendar_start).days
            logger.info(
                "🔥 Warming up cache from %s (%s days)...",
                calendar_start.isoformat(),
                effective_days,
            )
            await self._sync_recent_days(days=effective_days)
            await self.cache.recalculate_file_stats()
            await self.cache.set_last_full_load(
                datetime.now(), from_date=calendar_start, to_date=today
            )

        # Start background sync task (syncs recent days periodically)
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._initialized = True

        duration = time.time() - start_time
        stats = await self.cache.get_stats()

        logger.info(
            f"✅ PolicyService initialized in {duration:.2f}s", extra={"cache_stats": stats}
        )

        return stats

    async def shutdown(self) -> None:
        """Shutdown service: stop sync and disconnect cache."""
        logger.info("👋 Shutting down PolicyService...")

        # Cancel sync task
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        # Disconnect cache
        await self.cache.disconnect()

        logger.info("✅ PolicyService shutdown complete")

    # =========================================================================
    # Cache Loading
    # =========================================================================

    async def _sync_recent_days(self, days: int = 7) -> int:
        """Sync policies from the last N days into Redis cache.

        Stores full Policy objects (with endorsements and files)
        so cache responses are just as rich as DB responses.

        Args:
            days: Number of days to sync

        Returns:
            Count of policies synced
        """
        since_date = date.today() - timedelta(days=days)

        try:
            policies = await self.repo.get_since(since_date)

            if policies:
                policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in policies]
                count = await self.cache.set_policies_bulk(policy_dicts)
                logger.info("📦 Synced %s policies from last %s days", count, days)
                await self.cache.set_last_sync(datetime.now())
                return count
            else:
                logger.debug("📦 No policies found in last %s days", days)
                return 0

        except Exception as e:
            logger.error("❌ Sync failed: %s", e)
            return 0

    async def _sync_loop(self) -> None:
        """Background loop for syncing recent days.

        Only syncs last 7 days to catch new policies without overloading.
        """
        logger.info("🔄 Sync loop started (interval: %ss)", self._sync_interval)

        while True:
            try:
                await asyncio.sleep(self._sync_interval)
                await self._sync_recent_days(days=7)
            except asyncio.CancelledError:
                logger.info("🔄 Sync loop cancelled")
                break
            except Exception as e:
                logger.error("❌ Sync loop error: %s", e)
                # Continue running despite errors

    # =========================================================================
    # Policy queries — Unified entry point
    # =========================================================================

    async def list_policies(
        self,
        filters: PolicyFilterParams,
        pagination: PaginationParams,
    ) -> tuple[list[Policy], int]:
        """Unified policy listing with filtering and pagination.

        Encapsulates filter routing — the API layer calls only this method.
        Returns ``(policies, total_count)`` for ``PaginatedResponse`` construction.

        Filter priority:
            1. emission_date → cache-first by emission date
            2. creation_date (+ optional has_files) → cache-first by creation date
            3. start_date + end_date → cache-first date range
            4. no filter → all policies (cache/DB)
        """
        limit = pagination.limit
        skip = pagination.skip
        sort_order = filters.sort_order
        sort_by = filters.sort_by

        # ---- emission date ----
        if filters.emission_date:
            if filters.has_files or filters.has_unavailable:
                # Load all for the day, filter in Python, then paginate
                all_policies = await self.get_policies_by_emission_date(
                    filters.emission_date,
                    limit=10000,
                    offset=0,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
                filtered = all_policies
                if filters.has_files:
                    filtered = [p for p in filtered if p.has_files]
                if filters.has_unavailable:
                    filtered = [p for p in filtered if p.unavailable_file_count > 0]
                total = len(filtered)
                policies = filtered[skip : skip + limit]
            else:
                policies = await self.get_policies_by_emission_date(
                    filters.emission_date,
                    limit,
                    skip,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
                total = len(policies) if not skip else skip + len(policies)
            return policies, total

        # ---- creation date ----
        if filters.creation_date:
            if filters.has_unavailable:
                # Load all for the day, filter in Python, then paginate
                all_policies, _ = await self.get_policies_by_creation_date_counted(
                    filters.creation_date,
                    limit=10000,
                    offset=0,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
                filtered = [p for p in all_policies if p.unavailable_file_count > 0]
                if filters.has_files:
                    filtered = [p for p in filtered if p.has_files]
                total = len(filtered)
                policies = filtered[skip : skip + limit]
            elif filters.has_files:
                policies, total = await self.get_policies_with_files(
                    filters.creation_date,
                    limit=limit,
                    offset=skip,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
            else:
                policies, total = await self.get_policies_by_creation_date_counted(
                    filters.creation_date,
                    limit,
                    skip,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
            return policies, total

        # ---- date range ----
        if filters.start_date and filters.end_date:
            if filters.has_files or filters.has_unavailable:
                # Python-level filtering requires loading all matching records first.
                _CHUNK = 50_000
                _db_offset = 0
                all_filtered: list[Policy] = []
                while True:
                    chunk = await self.get_policies_by_date_range(
                        filters.start_date,
                        filters.end_date,
                        limit=_CHUNK,
                        offset=_db_offset,
                        order_dir=sort_order,
                        sort_by=sort_by,
                    )
                    if not chunk:
                        break
                    for p in chunk:
                        if filters.has_files and not p.has_files:
                            continue
                        if filters.has_unavailable and p.unavailable_file_count == 0:
                            continue
                        all_filtered.append(p)
                    if len(chunk) < _CHUNK:
                        break  # exhausted
                    _db_offset += _CHUNK
                total = len(all_filtered)
                policies = all_filtered[skip : skip + limit]
            else:
                policies = await self.get_policies_by_date_range(
                    filters.start_date,
                    filters.end_date,
                    limit,
                    skip,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
                total = len(policies) if not skip else skip + len(policies)
            return policies, total

        # ---- partial range → error ----
        if filters.start_date or filters.end_date:
            raise ServiceException(
                message="Provide both start_date and end_date for a range filter",
                status_code=400,
            )

        # ---- no filter → all policies ----
        if filters.has_unavailable:
            # No date scope + has_unavailable — scan full cache in chunks
            _CHUNK = 50_000
            _db_offset = 0
            all_filtered: list[Policy] = []
            while True:
                chunk = await self.get_all_policies(
                    _CHUNK,
                    _db_offset,
                    order_dir=sort_order,
                    sort_by=sort_by,
                )
                if not chunk:
                    break
                for p in chunk:
                    if filters.has_files and not p.has_files:
                        continue
                    if p.unavailable_file_count > 0:
                        all_filtered.append(p)
                if len(chunk) < _CHUNK:
                    break
                _db_offset += _CHUNK
            total = len(all_filtered)
            policies = all_filtered[skip : skip + limit]
            return policies, total

        policies = await self.get_all_policies(
            limit,
            skip,
            order_dir=sort_order,
            sort_by=sort_by,
        )
        total = await self.cache.get_all_count() or len(policies)
        return policies, total

    # =========================================================================
    # Individual query methods (used by list_policies, readiness, sync, etc.)
    # =========================================================================

    async def get_all_policies(
        self,
        limit: int = 100,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """List all policies with pagination. Cache-first using the all-policies ZSET.

        Strategy:
          - If the ZSET has enough entries to cover this page, serve from cache.
          - On miss (or too few entries), load from DB, populate cache, return page.
          - NOTE: Does not attempt to load the entire table on first call—only the
            requested page is loaded from DB when cache is cold (use /cache/warmup
            to pre-populate the ZSET).
        """
        _desc = order_dir.lower() == "desc"
        total_cached = await self.cache.get_all_count()
        if total_cached >= offset + limit:
            cached = await self.cache.get_all_paginated(offset, limit)
            if len(cached) == limit:
                logger.debug(
                    "📦 Cache hit: all-policies page offset=%d limit=%d (%d total indexed)",
                    offset,
                    limit,
                    total_cached,
                )
                cached.sort(
                    key=lambda p: p.get("emissionDate") or "",
                    reverse=_desc,
                )
                policies = [Policy.model_validate(p) for p in cached]
                await self._enrich_with_cached_storage(policies)
                return policies

        # Cache miss or stale entries—load this page from DB and cache each policy
        logger.debug(
            "🔍 Cache miss for all-policies offset=%d limit=%d (indexed=%d), querying DB...",
            offset,
            limit,
            total_cached,
        )
        policies = await self.repo.get_all(
            limit, offset, order_dir=order_dir.upper(), sort_by=sort_by
        )
        if policies:
            policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in policies]
            await self.cache.set_policies_bulk(policy_dicts)

        await self._enrich_with_cached_storage(policies)
        return policies

    async def get_policy(self, nro_pol: str) -> Policy:
        """Get complete policy by number.

        Checks cache first, falls back to DB.
        Automatically enriches with storage metadata from Redis.
        """
        # Try cache first
        cached = await self.cache.get_policy(nro_pol)

        if cached:
            logger.debug("📦 Cache hit for policy %s", nro_pol)
            policy = Policy.model_validate(cached)
        else:
            # Cache miss - get from DB and cache it
            policy = await self.repo.get_by_number(nro_pol)
            if policy:
                policy_dict = policy.model_dump(mode="json", by_alias=True)
                await self.cache.set_policy(nro_pol, policy_dict)

        if policy:
            await self._enrich_with_cached_storage([policy])

        return policy

    async def get_policies_by_emission_date(
        self,
        emission_date: date,
        limit: int = 1000,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by emission date. Cache-first."""
        _desc = order_dir.lower() == "desc"
        # Try cache
        cached_policies = await self.cache.get_policies_by_date(emission_date)

        if cached_policies:
            logger.debug("📦 Cache hit: %s policies for %s", len(cached_policies), emission_date)
            cached_policies.sort(
                key=lambda p: p.get("nroPol") or "",
                reverse=_desc,
            )
            policies = [Policy.model_validate(p) for p in cached_policies]
            policies = policies[offset : offset + limit]
        else:
            # Cache miss - load from DB and cache
            logger.debug("🔍 Cache miss for %s, querying DB...", emission_date)
            policies = await self.repo.get_by_emission_date(
                emission_date, limit, offset, order_dir=order_dir.upper(), sort_by=sort_by
            )

            if policies:
                policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in policies]
                await self.cache.set_policies_bulk(policy_dicts)

        await self._enrich_with_cached_storage(policies)
        return policies

    async def get_policies_by_creation_date(
        self,
        creation_date: date,
        limit: int = 1000,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by file creation date. Cache-first.

        Uses the polizas:fecha_creacion:{date} SET index populated by set_policies_bulk.
        On miss, loads from DB and populates the cache for future calls.
        """
        _desc = order_dir.lower() == "desc"
        cached = await self.cache.get_policies_by_creation_date(creation_date)
        if cached:
            logger.debug(
                "📦 Cache hit: %d policies for creation date %s",
                len(cached),
                creation_date,
            )
            cached.sort(key=lambda p: p.get("nroPol") or "", reverse=_desc)
            policies = [Policy.model_validate(p) for p in cached]
            policies = policies[offset : offset + limit]
        else:
            logger.debug("🔍 Cache miss for creation date %s, querying DB...", creation_date)
            # Load full day (no offset/limit) so we cache the complete set
            all_policies = await self.repo.get_by_creation_date(
                creation_date, limit=10000, offset=0, sort_by=sort_by
            )
            if all_policies:
                policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in all_policies]
                await self.cache.set_policies_bulk(policy_dicts)
            all_policies.sort(key=lambda p: p.nro_pol, reverse=_desc)
            policies = all_policies[offset : offset + limit]

        await self._enrich_with_cached_storage(policies)
        return policies

    async def get_policies_by_date_range(
        self,
        start_date: date,
        end_date: date,
        limit: int = 1000,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by emission date range. Cache-first, day by day.

        For each day in the range:
          - Hit is served directly from polizas:fecha:{date} SET.
          - Miss triggers a DB query for that day, which is then cached.

        For very large ranges (>60 days) falls back to a single DB query to
        avoid hundreds of individual Redis + DB round trips.
        """
        _desc = order_dir.lower() == "desc"
        days = (end_date - start_date).days

        if days > 60:
            # Large range: single DB query (too many round trips otherwise)
            logger.debug("🔍 Date range too large (%d days), querying DB directly...", days)
            policies = await self.repo.get_by_date_range(
                start_date, end_date, limit, offset, order_dir=order_dir.upper(), sort_by=sort_by
            )
            await self._enrich_with_cached_storage(policies)
            return policies

        # Small-to-medium range: cache-first day by day
        all_dicts: list[dict] = []
        current = start_date
        while current <= end_date:
            cached = await self.cache.get_policies_by_date(current)
            if cached:
                logger.debug("📦 Cache hit: %d policies for %s", len(cached), current)
                all_dicts.extend(cached)
            else:
                logger.debug("🔍 Cache miss for %s, querying DB...", current)
                day_policies = await self.repo.get_by_emission_date(current, limit=10000, offset=0)
                if day_policies:
                    day_dicts = [p.model_dump(mode="json", by_alias=True) for p in day_policies]
                    await self.cache.set_policies_bulk(day_dicts)
                    all_dicts.extend(day_dicts)
            current += timedelta(days=1)

        all_dicts.sort(key=lambda p: p.get("emissionDate") or "", reverse=_desc)
        policies = [Policy.model_validate(p) for p in all_dicts][offset : offset + limit]
        await self._enrich_with_cached_storage(policies)
        return policies

    async def get_policies_by_creation_date_counted(
        self,
        creation_date: date,
        limit: int = 1000,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> tuple[list[Policy], int]:
        """Get policies by creation date, returning (page, total_count)."""
        _desc = order_dir.lower() == "desc"
        cached = await self.cache.get_policies_by_creation_date(creation_date)
        if cached:
            logger.debug(
                "📦 Cache hit: %d policies for creation date %s",
                len(cached),
                creation_date,
            )
            cached.sort(key=lambda p: p.get("nroPol") or "", reverse=_desc)
            total = len(cached)
            policies = [Policy.model_validate(p) for p in cached[offset : offset + limit]]
        else:
            logger.debug("🔍 Cache miss for creation date %s, querying DB...", creation_date)
            all_policies = await self.repo.get_by_creation_date(
                creation_date, limit=10000, offset=0, sort_by=sort_by
            )
            if all_policies:
                policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in all_policies]
                await self.cache.set_policies_bulk(policy_dicts)
            all_policies.sort(key=lambda p: p.nro_pol, reverse=_desc)
            total = len(all_policies)
            policies = all_policies[offset : offset + limit]

        await self._enrich_with_cached_storage(policies)
        return policies, total

    async def get_policies_with_files(
        self,
        start_date: date,
        end_date: date | None = None,
        limit: int = 1000,
        offset: int = 0,
        order_dir: str = "desc",
        sort_by: str = "emission_date",
    ) -> tuple[list[Policy], int]:
        """Get policies that have PDF files, filtered by creation date.

        Uses repo.get_with_files() which filters at SQL level
        (WHERE txt_path IS NOT NULL) for efficiency.
        Falls back to cache-first path when cache is warm.

        Returns (page, total_count) for proper pagination.
        """
        _desc = order_dir.lower() == "desc"

        # Try cache first — filter has_files in Python from cached day
        cached = await self.cache.get_policies_by_creation_date(start_date)
        if cached:
            logger.debug(
                "📦 Cache hit: %d policies for creation date %s, filtering has_files",
                len(cached),
                start_date,
            )
            all_policies = [Policy.model_validate(p) for p in cached]
            with_files = [p for p in all_policies if p.has_files]
            with_files.sort(key=lambda p: p.nro_pol, reverse=_desc)
            total = len(with_files)
            policies = with_files[offset : offset + limit]
            await self._enrich_with_cached_storage(policies)
            return policies, total

        # Cache miss — use repo.get_with_files() (SQL-level txt_path IS NOT NULL)
        logger.debug(
            "🔍 Cache miss for creation date %s, querying DB with has_files...",
            start_date,
        )
        all_policies = await self.repo.get_with_files(
            start_date,
            end_date,
            limit=10000,
            offset=0,
            sort_by=sort_by,
        )
        # Also populate cache for the full day (so next call is cache-first)
        all_day = await self.repo.get_by_creation_date(start_date, limit=10000, offset=0)
        if all_day:
            policy_dicts = [p.model_dump(mode="json", by_alias=True) for p in all_day]
            await self.cache.set_policies_bulk(policy_dicts)

        all_policies.sort(key=lambda p: p.nro_pol, reverse=_desc)
        total = len(all_policies)
        policies = all_policies[offset : offset + limit]
        await self._enrich_with_cached_storage(policies)
        return policies, total

    # =========================================================================
    # Storage Metadata (Reverse Flow)
    # =========================================================================

    # Redis key prefix for storage metadata
    STORAGE_META_PREFIX = "storage:meta"

    def _storage_meta_key(self, policy_id: str, file_id: str) -> str:
        """Build Redis key for storage metadata."""
        return f"{self.STORAGE_META_PREFIX}:{policy_id}:{file_id}"

    async def save_storage_metadata(
        self,
        policy_id: str,
        file_id: str,
        metadata: StorageMetadataUpdate,
    ) -> StorageMetadataResponse:
        """Save storage metadata for a policy file.

        Called by the dispatcher service after uploading a file to storage.
        Stores in Redis with no TTL (persistent until explicitly deleted).

        Args:
            policy_id: Policy number (nro_pol)
            file_id: File name (unique per endorsement)
            metadata: Storage ID and link from storage service

        Returns:
            Confirmation response
        """
        key = self._storage_meta_key(policy_id, file_id)
        value = json.dumps(
            {
                "storage_id": metadata.storage_id,
                "storage_link": metadata.storage_link,
                "updated_at": datetime.now().isoformat(),
            }
        )
        await self.cache.redis.set(key, value)
        logger.debug("💾 Storage metadata saved for %s/%s", policy_id, file_id)

        return StorageMetadataResponse(
            policy_id=policy_id,
            file_id=file_id,
            storage_id=metadata.storage_id,
            storage_link=metadata.storage_link,
        )

    async def get_storage_metadata(self, policy_id: str, file_id: str) -> dict | None:
        """Get storage metadata for a policy file from Redis."""
        key = self._storage_meta_key(policy_id, file_id)
        raw = await self.cache.redis.get(key)
        if raw:
            return json.loads(raw)
        return None

    async def delete_storage_metadata(self, policy_id: str, file_id: str) -> bool:
        """Delete storage metadata for a policy file."""
        key = self._storage_meta_key(policy_id, file_id)
        deleted = await self.cache.redis.delete(key)
        if deleted:
            logger.debug("🗑️ Storage metadata deleted for %s/%s", policy_id, file_id)
        return deleted > 0

    # =========================================================================
    # Mark sent (write to timpresion_pdf)
    # =========================================================================

    async def mark_sent(self, nro_pol_list: list[str], sent_date: date) -> int:
        """Mark timpresion_pdf rows as sent for the given policy numbers.

        Returns the number of updated rows.
        """
        return await self.repo.mark_sent(nro_pol_list, sent_date)

    async def _enrich_with_cached_storage(self, policies: list[Policy]) -> None:
        """Enrich policy files with storage metadata from Redis.

        Reads cached metadata written by the dispatcher via PATCH.
        Modifies policies in-place. Cost: ~1ms per policy (local Redis read).
        No external service calls.
        """
        if not policies:
            return

        # Collect all files that could have storage metadata
        lookups: list[tuple[str, str, list]] = []  # (policy_id, file_name, file_ref)
        for policy in policies:
            for endorsement in policy.endorsements:
                for file in endorsement.files:
                    if file.available and file.name:
                        lookups.append((policy.nro_pol, file.name, file))

        if not lookups:
            return

        # Batch read from Redis using pipeline
        try:
            pipe = self.cache.redis.pipeline()
            for policy_id, file_name, _ in lookups:
                pipe.get(self._storage_meta_key(policy_id, file_name))
            results = await pipe.execute()

            for (_, _, file_ref), raw in zip(lookups, results, strict=False):
                if raw:
                    meta = json.loads(raw)
                    sid = meta.get("storage_id")
                    # "NOT_FOUND" es un sentinel del dispatcher — el archivo no
                    # existe en disco. No contar como in_storage.
                    if sid and sid != "NOT_FOUND":
                        file_ref.in_storage = True
                        file_ref.storage_id = sid
                        file_ref.storage_link = meta.get("storage_link")
        except Exception as e:
            logger.warning("⚠️ Storage metadata enrichment failed: %s", e)

    # =========================================================================
    # Readiness — computed view over policies
    # =========================================================================

    async def check_readiness(
        self,
        emission_date: date,
        include_missing: bool = True,
        refresh: bool = False,
    ) -> ReadinessResponse:
        """Check if all policies for a date are ready (files available AND in storage).

        Data sources:
        1. Direct COUNT from pv_header — stable baseline (emitted_total)
        2. Cache-first policy query — per-policy file/storage status
        3. NOT EXISTS query — policies with zero timpresion_pdf rows (no PDF yet)

        Key design: the paginated query finds policies by fec_emi = target_date (CTE)
        but returns ALL endorsements for those policies (main POLICY_BASE join).
        We scope evaluation to endorsements matching emission_date to avoid false
        positives from unavailable files on historical endorsements of the same policy.

        A policy counts as ready for this date when:
        - The endorsement(s) emitted on this date have at least one available file
        - ALL available files from those endorsements are in cloud storage
        """
        if refresh:
            await self.invalidate_date_cache(emission_date)

        # Stable baseline — direct DB count, no file join, bypasses cache
        emitted_total = await self.repo.count_by_emission_date(emission_date)

        # Per-policy file status from cache (cache may return all endorsements for
        # each matching policy — we scope below to only the target-date endorsements)
        policies = await self.get_policies_by_emission_date(
            emission_date,
            limit=5000,
            offset=0,
        )

        # Policies confirmed to have no timpresion_pdf rows for the target endorsement.
        # NOT EXISTS is date-scoped via id_pv — reliable regardless of cache freshness.
        # pv_header has one row per nro_endoso; deduplicate by nro_pol.
        no_files_rows = await self.repo.get_no_files_by_emission_date(emission_date)
        truly_no_files: dict[str, dict] = {}
        for row in no_files_rows:
            nro = str(row["nro_pol"])
            if nro not in truly_no_files:
                truly_no_files[nro] = row

        # Evaluate each cached policy against its target-date endorsements only.
        # Older endorsements (different fec_emi) are irrelevant for today's readiness.
        complete = 0
        in_storage_count = 0
        unavailable_in_cache: list[Policy] = []
        cache_resolved: set[str] = set()  # nro_pol values given a definitive answer

        for p in policies:
            # Files from endorsements emitted on the target date only
            date_avail = [
                f
                for e in p.endorsements
                if e.emission_date == emission_date
                for f in e.files
                if f.available
            ]
            date_all = [
                f for e in p.endorsements if e.emission_date == emission_date for f in e.files
            ]

            if date_avail:
                cache_resolved.add(p.nro_pol)
                complete += 1
                if all(f.in_storage for f in date_avail):
                    in_storage_count += 1
            elif date_all:
                # Has file records for this date's endorsement but ALL are unavailable
                cache_resolved.add(p.nro_pol)
                unavailable_in_cache.append(p)
            # else: policy is in cache (from older sync) but target-date endorsement
            # has no files — let truly_no_files handle it

        not_in_storage_count = complete - in_storage_count

        # Exclude cache-resolved policies from the DB-derived counts to avoid
        # double-counting (e.g. a policy with two endorsements on the same date
        # where one has files and one doesn't — the cache already resolved it).
        no_files_yet_count = sum(1 for nro in truly_no_files if nro not in cache_resolved)
        missing_count = no_files_yet_count + len(unavailable_in_cache)

        missing_policies: list[MissingPolicyItem] = []
        if include_missing:
            # 1. In cache: target-date endorsements have files but ALL are unavailable/excluded
            for p in unavailable_in_cache:
                date_all = [
                    f for e in p.endorsements if e.emission_date == emission_date for f in e.files
                ]
                has_unavailable_marker = any(
                    f.original_path and f.original_path.upper().startswith("ARCHIVO")
                    for f in date_all
                )
                missing_policies.append(
                    MissingPolicyItem(
                        nro_pol=p.nro_pol,
                        ramo=p.ramo,
                        insured_name=p.insured.name if p.insured else "",
                        producer_name=p.producer.name if p.producer else "",
                        reason="UNAVAILABLE" if has_unavailable_marker else "EXCLUDED",
                    )
                )

            # 2. In cache: target-date endorsements have available files but not all in Storage
            for p in policies:
                if p.nro_pol not in cache_resolved:
                    continue
                date_avail = [
                    f
                    for e in p.endorsements
                    if e.emission_date == emission_date
                    for f in e.files
                    if f.available
                ]
                if date_avail and not all(f.in_storage for f in date_avail):
                    storage_count = sum(1 for f in date_avail if f.in_storage)
                    missing_policies.append(
                        MissingPolicyItem(
                            nro_pol=p.nro_pol,
                            ramo=p.ramo,
                            insured_name=p.insured.name if p.insured else "",
                            producer_name=p.producer.name if p.producer else "",
                            reason="NOT_IN_STORAGE",
                            file_count=len(date_avail),
                            files_in_storage=storage_count,
                        )
                    )

            # 3. From DB: no PDF at all (NOT EXISTS) — skip if cache already resolved
            for nro_pol, row in truly_no_files.items():
                if nro_pol in cache_resolved:
                    continue
                missing_policies.append(
                    MissingPolicyItem(
                        nro_pol=nro_pol,
                        ramo=row.get("ramo") or "",
                        insured_name=row.get("nombre_asegurado") or "",
                        producer_name=row.get("nombre_agente") or "",
                        reason="NO_FILES",
                    )
                )

        return ReadinessResponse(
            date=emission_date,
            total=emitted_total,
            with_files=emitted_total - no_files_yet_count,
            no_files_yet=no_files_yet_count,
            complete=complete,
            missing=missing_count,
            in_storage=in_storage_count,
            not_in_storage=not_in_storage_count,
            ready=emitted_total > 0 and missing_count == 0 and not_in_storage_count == 0,
            missing_policies=missing_policies,
        )

    # =========================================================================
    # Producer/Insured queries
    # =========================================================================

    async def get_producer(self, code: str) -> ProducerResponse:
        """Get producer by code."""
        return await self.repo.get_producer(code)

    async def get_producers(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        order_dir: str = "asc",
    ) -> list[ProducerResponse]:
        """Get list of producers with optional name/email search."""
        return await self.repo.get_producers_list(limit, offset, search, order_dir)

    async def get_insured(self, code: str) -> InsuredResponse:
        """Get insured by insured code (cod_aseg)."""
        return await self.repo.get_insured(code)

    async def get_insureds(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        order_dir: str = "asc",
    ) -> list[InsuredResponse]:
        """Get list of insureds with optional name/email search."""
        return await self.repo.get_insureds_list(limit, offset, search, order_dir)

    # =========================================================================
    # Cache operations
    # =========================================================================

    async def get_cache_stats(self) -> dict:
        """Get Redis cache statistics."""
        return await self.cache.get_stats()

    async def get_cache_diagnostics(self) -> dict:
        """Get detailed file sync diagnostic report."""
        return await self.cache.get_diagnostics()

    async def refresh_cache(self, days: int = 30) -> dict:
        """Refresh cache with policies from the last N days.

        Does NOT clear existing cache - just updates/adds recent policies.
        Use clear_cache() first if you want a fresh start.

        Args:
            days: Number of days to refresh (default: 30)
        """
        logger.info("🔄 Refreshing cache with last %s days...", days)
        start_time = time.time()

        count = await self._sync_recent_days(days=days)
        await self.cache.recalculate_file_stats()

        duration = time.time() - start_time
        logger.info("✅ Cache refresh complete: %s policies in %ss", count, duration)

        return await self.cache.get_stats()

    async def warmup_cache(self, days: int = 7) -> CacheWarmupResponse:
        """Warm up cache with policies from the last N days.

        Call this after startup to preload frequently accessed data.
        Recommended: 7-30 days depending on typical query patterns.

        Args:
            days: Number of days to preload (default: 7)
        """
        logger.info("🔥 Warming up cache with last %s days...", days)
        start_time = time.time()

        count = await self._sync_recent_days(days=days)
        await self.cache.recalculate_file_stats()

        duration = time.time() - start_time
        logger.info("✅ Cache warmup complete: %s policies in %ss", count, duration)

        return CacheWarmupResponse(
            success=True,
            policies_cached=count,
            duration_seconds=duration,
        )

    async def clear_cache(self) -> int:
        """Clear all policy data from cache.

        Returns:
            Count of keys deleted
        """
        logger.info("🗑️ Clearing policy cache...")
        count = await self.cache.clear_all_policies()
        logger.info("✅ Cache cleared: %s keys deleted", count)
        return count

    async def invalidate_policy_cache(self, nro_pol: str) -> bool:
        """Invalidate cache for a specific policy (force reload from DB)."""
        try:
            # Delete from cache
            await self.cache.delete(self.cache._policy_key(nro_pol))

            # Reload from DB and cache the full Policy
            policy = await self.repo.get_by_number(nro_pol)
            if policy:
                policy_dict = policy.model_dump(mode="json", by_alias=True)
                await self.cache.set_policy(nro_pol, policy_dict)

            return True
        except Exception as e:
            logger.error("Failed to invalidate policy %s: %s", nro_pol, e)
            return False

    async def invalidate_date_cache(self, target_date: date) -> bool:
        """Invalidate cache for a specific date."""
        await self.cache.delete(self.cache._policies_date_key(target_date))
        return True
