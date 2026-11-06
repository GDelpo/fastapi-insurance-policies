"""Redis cache client for policy data.

Provides:
- Full policy cache with indices
- Short-lived cache for readiness status
- Automatic TTL management
"""

import json
from datetime import date, datetime
from typing import Any

import redis.asyncio as redis

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)


class CacheManager:
    """Async Redis cache manager with policy indexing."""

    # Key prefixes
    PREFIX_POLICY = "poliza:"
    PREFIX_POLICIES_DATE = "polizas:fecha:"
    PREFIX_POLICIES_CREATION_DATE = "polizas:fecha_creacion:"  # indexed by pdf.fecha
    PREFIX_POLICIES_PRODUCER = "polizas:productor:"
    PREFIX_POLICIES_INSURED = "polizas:asegurado:"
    PREFIX_POLICIES_RAMO = "polizas:ramo:"
    PREFIX_METADATA = "cache:meta:"
    KEY_ALL_POLICIES = "polizas:all"  # ZSET: all nro_pol (score=0, lexicographic order)
    KEY_FILE_STATS = "stats:file_counts"  # JSON blob with file counters
    PREFIX_STORAGE_META = "storage:meta"  # storage metadata keys for counting

    def __init__(self, redis_url: str):
        """Initialize cache manager."""
        self.redis_url = redis_url
        self._client: redis.Redis | None = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to Redis."""
        try:
            self._client = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=50,
            )
            await self._client.ping()
            self._connected = True
            logger.info("✅ Redis cache connected")
            return True
        except Exception as e:
            logger.warning("⚠️ Redis connection failed: %s", e)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._client:
            await self._client.close()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if connected to Redis."""
        return self._connected

    @property
    def redis(self) -> redis.Redis:
        """Expose the raw Redis client for direct operations.

        Used by service methods that need raw access (e.g., storage metadata
        stored without TTL, pipeline batches).
        """
        if not self._client:
            raise RuntimeError("Redis client not connected")
        return self._client

    # =========================================================================
    # Basic operations
    # =========================================================================

    async def get(self, key: str) -> Any | None:
        """Get value from cache."""
        if not self._connected or not self._client:
            return None
        try:
            value = await self._client.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.warning("Cache get error: %s", e)
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Set value in cache."""
        if not self._connected or not self._client:
            return False
        try:
            ttl = settings.cache_ttl_hours * 3600 if ttl_seconds is None else ttl_seconds
            await self._client.set(key, json.dumps(value), ex=ttl)
            return True
        except Exception as e:
            logger.warning("Cache set error: %s", e)
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if not self._connected or not self._client:
            return False
        try:
            await self._client.delete(key)
            return True
        except Exception as e:
            logger.warning("Cache delete error: %s", e)
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern."""
        if not self._connected or not self._client:
            return 0
        try:
            deleted = 0
            async for key in self._client.scan_iter(match=pattern, count=200):
                deleted += await self._client.delete(key)
            return deleted
        except Exception as e:
            logger.warning("Cache delete pattern error: %s", e)
            return 0

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if not self._connected or not self._client:
            return False
        try:
            return bool(await self._client.exists(key))
        except Exception as e:
            logger.warning("Cache exists error: %s", e)
            return False

    # =========================================================================
    # Key generators
    # =========================================================================

    def _policy_key(self, nro_pol: str) -> str:
        return f"{self.PREFIX_POLICY}{nro_pol}"

    def _policies_date_key(self, fecha: date) -> str:
        return f"{self.PREFIX_POLICIES_DATE}{fecha.isoformat()}"

    def _policies_producer_key(self, producer_code: str) -> str:
        return f"{self.PREFIX_POLICIES_PRODUCER}{producer_code}"

    def _policies_insured_key(self, insured_code: str) -> str:
        return f"{self.PREFIX_POLICIES_INSURED}{insured_code}"

    def _policies_ramo_key(self, ramo: str) -> str:
        return f"{self.PREFIX_POLICIES_RAMO}{ramo}"

    def _policies_creation_date_key(self, fecha: date) -> str:
        return f"{self.PREFIX_POLICIES_CREATION_DATE}{fecha.isoformat()}"

    # =========================================================================
    # Policy CRUD
    # =========================================================================

    async def get_policy(self, nro_pol: str) -> dict | None:
        """Get policy from cache."""
        return await self.get(self._policy_key(nro_pol))

    async def set_policy(
        self, nro_pol: str, policy_data: dict, update_indices: bool = True
    ) -> bool:
        """Set policy in cache with optional index updates."""
        if not self._connected or not self._client:
            return False

        try:
            # Store policy data
            ttl = settings.cache_ttl_hours * 3600
            await self._client.set(self._policy_key(nro_pol), json.dumps(policy_data), ex=ttl)

            # Update indices
            if update_indices:
                await self._index_policy(nro_pol, policy_data, ttl)

            return True
        except Exception as e:
            logger.warning("Cache set_policy error: %s", e)
            return False

    async def _index_policy(self, nro_pol: str, policy_data: dict, ttl: int) -> None:
        """Add policy to secondary indices (Redis SETs).

        Navigates nested Policy hierarchy:
        - endorsements[*].emissionDate: per-endorsement dates (renewal-safe)
        - producer.code: nested producer object
        - insured.code: nested insured object
        - ramo: top-level field
        """
        if not self._client:
            return

        pipe = self._client.pipeline()

        # Index by each endorsement's emission date.
        # Using per-endorsement dates (not top-level emissionDate which reflects
        # endorsement 0) ensures renewal policies are findable by their renewal date.
        for endorsement in policy_data.get("endorsements", []):
            end_emi = endorsement.get("emissionDate")
            if end_emi:
                try:
                    date_key = self._policies_date_key(date.fromisoformat(end_emi))
                    pipe.sadd(date_key, nro_pol)
                    pipe.expire(date_key, ttl)
                except ValueError:
                    pass

        # Index by producer (nested object)
        producer = policy_data.get("producer", {})
        producer_code = producer.get("code") if isinstance(producer, dict) else None
        if producer_code:
            prod_key = self._policies_producer_key(producer_code)
            pipe.sadd(prod_key, nro_pol)
            pipe.expire(prod_key, ttl)

        # Index by insured (nested object)
        insured = policy_data.get("insured", {})
        insured_code = insured.get("code") if isinstance(insured, dict) else None
        if insured_code:
            ins_key = self._policies_insured_key(insured_code)
            pipe.sadd(ins_key, nro_pol)
            pipe.expire(ins_key, ttl)

        # Index by ramo
        if policy_data.get("ramo"):
            ramo_key = self._policies_ramo_key(policy_data["ramo"])
            pipe.sadd(ramo_key, nro_pol)
            pipe.expire(ramo_key, ttl)

        await pipe.execute()

    async def set_policies_bulk(self, policies: list[dict]) -> int:
        """Bulk set policies with indexing. Returns count of policies cached.

        Maintains indices:
          - poliza:{nro_pol}              individual policy data
          - polizas:fecha:{date}          SET of nro_pol by emission date
          - polizas:fecha_creacion:{date} SET of nro_pol by PDF creation date
          - polizas:productor:{code}       SET of nro_pol by producer
          - polizas:asegurado:{code}       SET of nro_pol by insured
          - polizas:all                   ZSET of all nro_pol (lexicographic)
        """
        if not self._connected or not self._client:
            return 0

        try:
            ttl = settings.cache_ttl_hours * 3600
            count = 0

            batch_size = 100
            for i in range(0, len(policies), batch_size):
                batch = policies[i : i + batch_size]
                pipe = self._client.pipeline()

                for policy_data in batch:
                    nro_pol = policy_data.get("nroPol")
                    if not nro_pol:
                        continue

                    # ── individual policy ─────────────────────────────────
                    pipe.set(self._policy_key(nro_pol), json.dumps(policy_data), ex=ttl)

                    # ── all-policies ZSET (score=0 → lexicographic order) ─
                    pipe.zadd(self.KEY_ALL_POLICIES, {nro_pol: 0})

                    # ── emission date index (per endorsement) ─────────────
                    # Index by each endorsement's date so renewal policies
                    # are findable by their renewal date, not just the
                    # original-policy date (endorsement 0).
                    for endorsement in policy_data.get("endorsements", []):
                        end_emi = endorsement.get("emissionDate")
                        if end_emi:
                            try:
                                dk = self._policies_date_key(date.fromisoformat(end_emi))
                                pipe.sadd(dk, nro_pol)
                                pipe.expire(dk, ttl)
                            except ValueError:
                                pass

                    # ── PDF creation date index (per file) ────────────────
                    for endorsement in policy_data.get("endorsements", []):
                        for file_data in endorsement.get("files", []):
                            created_at = file_data.get("createdAt")
                            if created_at:
                                try:
                                    ck = self._policies_creation_date_key(
                                        date.fromisoformat(str(created_at)[:10])
                                    )
                                    pipe.sadd(ck, nro_pol)
                                    pipe.expire(ck, ttl)
                                except (ValueError, AttributeError):
                                    pass

                    # ── producer index ────────────────────────────────────
                    producer = policy_data.get("producer", {})
                    producer_code = producer.get("code") if isinstance(producer, dict) else None
                    if producer_code:
                        pk = self._policies_producer_key(producer_code)
                        pipe.sadd(pk, nro_pol)
                        pipe.expire(pk, ttl)

                    # ── insured index ─────────────────────────────────────
                    insured = policy_data.get("insured", {})
                    insured_code = insured.get("code") if isinstance(insured, dict) else None
                    if insured_code:
                        ik = self._policies_insured_key(insured_code)
                        pipe.sadd(ik, nro_pol)
                        pipe.expire(ik, ttl)

                    count += 1

                await pipe.execute()

            # Set TTL on the all-policies ZSET after batch population
            if count > 0:
                await self._client.expire(self.KEY_ALL_POLICIES, ttl)

            return count

        except Exception as e:
            logger.error("Cache bulk set error: %s", e)
            return 0

    # =========================================================================
    # Policy queries by index
    # =========================================================================

    async def get_policies_by_date(self, fecha: date) -> list[dict]:
        """Get all policies for a date."""
        if not self._connected or not self._client:
            return []

        try:
            # Get policy numbers from date index
            date_key = self._policies_date_key(fecha)
            nro_pols = await self._client.smembers(date_key)

            if not nro_pols:
                return []

            # Get policy data for each
            pipe = self._client.pipeline()
            for nro_pol in nro_pols:
                pipe.get(self._policy_key(nro_pol))

            results = await pipe.execute()

            policies = []
            for data in results:
                if data:
                    policies.append(json.loads(data))

            return policies

        except Exception as e:
            logger.warning("Cache get_policies_by_date error: %s", e)
            return []

    async def get_policies_by_creation_date(self, fecha: date) -> list[dict]:
        """Get all policies whose PDF was created on this date."""
        if not self._connected or not self._client:
            return []
        try:
            key = self._policies_creation_date_key(fecha)
            nro_pols = await self._client.smembers(key)
            if not nro_pols:
                return []
            pipe = self._client.pipeline()
            for nro_pol in nro_pols:
                pipe.get(self._policy_key(nro_pol))
            results = await pipe.execute()
            return [json.loads(data) for data in results if data]
        except Exception as e:
            logger.warning("Cache get_policies_by_creation_date error: %s", e)
            return []

    async def get_all_count(self) -> int:
        """Count of policies in the all-policies ZSET (ZCARD)."""
        if not self._connected or not self._client:
            return 0
        try:
            return await self._client.zcard(self.KEY_ALL_POLICIES)
        except Exception:
            return 0

    async def get_all_paginated(self, offset: int, limit: int) -> list[dict]:
        """Return a page of policies from the all-policies ZSET.

        Uses ZRANGEBYLEX for O(log N + M) lexicographic pagination.
        Falls back gracefully if any individual policy key has expired.
        """
        if not self._connected or not self._client:
            return []
        try:
            nro_pols = await self._client.zrangebylex(
                self.KEY_ALL_POLICIES, "-", "+", start=offset, num=limit
            )
            if not nro_pols:
                return []
            pipe = self._client.pipeline()
            for nro_pol in nro_pols:
                pipe.get(self._policy_key(nro_pol))
            results = await pipe.execute()
            return [json.loads(data) for data in results if data]
        except Exception as e:
            logger.warning("Cache get_all_paginated error: %s", e)
            return []

    # =========================================================================
    # Metadata
    # =========================================================================

    async def get_last_full_load(self) -> datetime | None:
        """Get timestamp of last full cache load."""
        data = await self.get(f"{self.PREFIX_METADATA}last_full_load")
        if data:
            return datetime.fromisoformat(data)
        return None

    async def set_last_full_load(
        self, timestamp: datetime, from_date: date | None = None, to_date: date | None = None
    ) -> bool:
        """Record that a full cache load completed, including the date range loaded."""
        if not self._connected or not self._client:
            return False
        try:
            await self._client.set(
                f"{self.PREFIX_METADATA}last_full_load",
                json.dumps(timestamp.isoformat()),
            )
            if from_date:
                await self._client.set(
                    f"{self.PREFIX_METADATA}cache_from",
                    json.dumps(from_date.isoformat()),
                )
            if to_date:
                await self._client.set(
                    f"{self.PREFIX_METADATA}cache_to",
                    json.dumps(to_date.isoformat()),
                )
            return True
        except Exception:
            return False

    async def get_cache_date_range(self) -> dict[str, str | None]:
        """Get the date range currently loaded in cache."""
        from_raw = await self.get(f"{self.PREFIX_METADATA}cache_from")
        to_raw = await self.get(f"{self.PREFIX_METADATA}cache_to")
        return {"from": from_raw, "to": to_raw}

    async def get_last_sync(self) -> datetime | None:
        """Get timestamp of last incremental sync."""
        data = await self.get(f"{self.PREFIX_METADATA}last_sync")
        if data:
            return datetime.fromisoformat(data)
        return None

    async def set_last_sync(self, timestamp: datetime) -> bool:
        """Set timestamp of last incremental sync."""
        if not self._connected or not self._client:
            return False
        try:
            await self._client.set(
                f"{self.PREFIX_METADATA}last_sync", json.dumps(timestamp.isoformat())
            )
            return True
        except Exception:
            return False

    # =========================================================================
    # Stats
    # =========================================================================

    async def get_stats(self) -> dict:
        """Get cache statistics including file counters and diagnostic fields.

        Diagnostic fields added:
        - coverage_pct: % of available files already uploaded to storage
        - files_pending_pct: inverse of coverage_pct (easy to spot at a glance)
        - sync_age_seconds: seconds since last incremental sync (None if never synced)
        - cache_date_range: {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} loaded range
        - last_full_load: timestamp of last complete warmup (null = never ran or Redis restart)
        """
        if not self._connected or not self._client:
            return {"connected": False}

        try:
            # Count policy keys using ZCARD on the all-policies ZSET (O(1))
            policy_count = await self._client.zcard(self.KEY_ALL_POLICIES)

            # Count date and producer indices with SCAN
            date_count = 0
            async for _ in self._client.scan_iter(match=f"{self.PREFIX_POLICIES_DATE}*", count=500):
                date_count += 1
            producer_count = 0
            async for _ in self._client.scan_iter(
                match=f"{self.PREFIX_POLICIES_PRODUCER}*", count=500
            ):
                producer_count += 1

            # Read pre-calculated file stats blob (O(1))
            file_stats = {}
            raw = await self._client.get(self.KEY_FILE_STATS)
            if raw:
                file_stats = json.loads(raw)

            # Count storage metadata keys (files uploaded to storage)
            storage_count = 0
            async for _ in self._client.scan_iter(match=f"{self.PREFIX_STORAGE_META}:*", count=500):
                storage_count += 1

            available_files = file_stats.get("available_files", 0)
            files_pending = max(0, available_files - storage_count)

            # Coverage percentage
            coverage_pct = (
                round(storage_count / available_files * 100, 1) if available_files > 0 else None
            )

            last_full_load = await self.get_last_full_load()
            last_sync = await self.get_last_sync()
            cache_date_range = await self.get_cache_date_range()

            # How stale is the cache?
            now = datetime.now()
            sync_age_seconds = int((now - last_sync).total_seconds()) if last_sync else None

            return {
                "connected": True,
                "total_policies": policy_count,
                "unique_dates": date_count,
                "unique_producers": producer_count,
                "total_files": file_stats.get("total_files", 0),
                "available_files": available_files,
                "unavailable_files": file_stats.get("unavailable_files", 0),
                "files_in_storage": storage_count,
                "files_pending": files_pending,
                "coverage_pct": coverage_pct,
                "cache_date_range": cache_date_range,
                # Append "Z" so browsers parse these as UTC, not local time.
                # Stored datetimes are naive UTC (server runs in UTC container).
                "last_full_load": (last_full_load.isoformat() + "Z") if last_full_load else None,
                "last_sync": (last_sync.isoformat() + "Z") if last_sync else None,
                "sync_age_seconds": sync_age_seconds,
            }
        except Exception as e:
            logger.warning("Cache get_stats error: %s", e)
            return {"connected": True, "error": str(e)}

    async def get_diagnostics(self) -> dict:
        """Scan all cached policies and build a file sync diagnostic report.

        Identifies:
        - Pending files: available on disk (available=True) but not yet in storage.
          Bucketed by file age using createdAt.
        - Unavailable files: marked as 'ARCHIVO NO DISPONIBLE' (available=False).

        This performs a full Redis scan — may take several seconds on large caches.
        """
        if not self._connected or not self._client:
            return {"connected": False}

        from datetime import date as _date

        today = _date.today()

        # Pass 1: collect all files categorized by availability
        available_files: list[
            tuple[str, str, str, str | None]
        ] = []  # (nro_pol, producer_code, file_name, created_at)
        unavailable_count = 0
        unavailable_sample: list[dict] = []

        async for key in self._client.scan_iter(match=f"{self.PREFIX_POLICY}*", count=500):
            raw = await self._client.get(key)
            if not raw:
                continue
            try:
                policy = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            nro_pol = policy.get("nroPol", "")
            producer_code = (policy.get("producer") or {}).get("code", "")

            for endorsement in policy.get("endorsements", []):
                for f in endorsement.get("files", []):
                    file_name = f.get("name", "")
                    if not f.get("available", True):
                        unavailable_count += 1
                        if len(unavailable_sample) < 10:
                            unavailable_sample.append(
                                {
                                    "nro_pol": nro_pol,
                                    "producer_code": producer_code,
                                    "file_name": file_name,
                                }
                            )
                    else:
                        available_files.append(
                            (nro_pol, producer_code, file_name, f.get("createdAt"))
                        )

        # Pass 2: batch-check which available files are missing from storage
        pending: list[tuple[str, str, str, str | None]] = []
        batch_size = 500
        for i in range(0, len(available_files), batch_size):
            batch = available_files[i : i + batch_size]
            pipe = self._client.pipeline()
            for nro_pol, _, file_name, _ in batch:
                pipe.exists(f"{self.PREFIX_STORAGE_META}:{nro_pol}:{file_name}")
            results = await pipe.execute()
            for entry, in_storage in zip(batch, results, strict=False):
                if not in_storage:
                    pending.append(entry)

        # Bucket pending files by age using createdAt
        buckets: dict[str, int] = {
            "last_7d": 0,
            "days_8_30": 0,
            "days_31_90": 0,
            "over_90d": 0,
            "unknown_date": 0,
        }
        pending_sample: list[dict] = []

        for nro_pol, producer_code, file_name, created_at in pending:
            if len(pending_sample) < 20:
                pending_sample.append(
                    {
                        "nro_pol": nro_pol,
                        "producer_code": producer_code,
                        "file_name": file_name,
                        "created_at": created_at,
                    }
                )
            if not created_at:
                buckets["unknown_date"] += 1
                continue
            try:
                file_date = _date.fromisoformat(str(created_at)[:10])
                age_days = (today - file_date).days
                if age_days <= 7:
                    buckets["last_7d"] += 1
                elif age_days <= 30:
                    buckets["days_8_30"] += 1
                elif age_days <= 90:
                    buckets["days_31_90"] += 1
                else:
                    buckets["over_90d"] += 1
            except (ValueError, TypeError):
                buckets["unknown_date"] += 1

        return {
            "connected": True,
            "pending_count": len(pending),
            "unavailable_count": unavailable_count,
            "available_total": len(available_files),
            "buckets": buckets,
            "pending_sample": pending_sample,
            "unavailable_sample": unavailable_sample,
        }

    async def recalculate_file_stats(self) -> dict:
        """Scan all cached policies and compute file counters.

        Stores the result as a JSON blob in Redis so get_stats() is O(1).
        Called after warmup / refresh / clear operations.
        """
        if not self._connected or not self._client:
            return {}

        total = 0
        available = 0
        unavailable = 0

        async for key in self._client.scan_iter(match=f"{self.PREFIX_POLICY}*", count=500):
            raw = await self._client.get(key)
            if not raw:
                continue
            try:
                policy = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for endorsement in policy.get("endorsements", []):
                for f in endorsement.get("files", []):
                    total += 1
                    if f.get("available", True):
                        available += 1
                    else:
                        unavailable += 1

        stats = {
            "total_files": total,
            "available_files": available,
            "unavailable_files": unavailable,
        }
        ttl = settings.cache_ttl_hours * 3600
        await self._client.set(self.KEY_FILE_STATS, json.dumps(stats), ex=ttl)
        logger.info(
            "📊 File stats recalculated: %s total, %s available, %s unavailable",
            total,
            available,
            unavailable,
        )
        return stats

    async def clear_all_policies(self) -> int:
        """Clear all policy data from cache. Returns count of keys deleted."""
        if not self._connected or not self._client:
            return 0

        try:
            count = 0
            count += await self.delete_pattern(f"{self.PREFIX_POLICY}*")
            count += await self.delete_pattern(f"{self.PREFIX_POLICIES_DATE}*")
            count += await self.delete_pattern(f"{self.PREFIX_POLICIES_CREATION_DATE}*")
            count += await self.delete_pattern(f"{self.PREFIX_POLICIES_PRODUCER}*")
            count += await self.delete_pattern(f"{self.PREFIX_POLICIES_INSURED}*")
            count += await self.delete_pattern(f"{self.PREFIX_POLICIES_RAMO}*")
            count += await self.delete(self.KEY_ALL_POLICIES)
            await self._client.delete(self.KEY_FILE_STATS)
            logger.info("🗑️ Cleared %s cache keys", count)
            return count
        except Exception as e:
            logger.error("Cache clear error: %s", e)
            return 0
