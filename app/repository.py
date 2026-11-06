"""Repository for policy data access.

Responsibility: orchestrate data access only.
  - Receives a SQL client
  - Calls the right query from queries.py
  - Passes rows to the right mapper from queries.py
  - Returns typed domain objects

All SQL lives in queries.py.
All domain mapping lives in queries.py.
This class only decides which query to run and handles errors.
"""

from datetime import date, datetime

from app.config import settings
from app.exceptions import EntityNotFoundError
from app.logger import get_logger
from app.queries import (
    COUNT_EMITTED_BY_DATE,
    INSURED_BY_CODE,
    POLICIES_NO_FILES_BY_DATE,
    POLICY_BY_NUMBER,
    POLICY_SINCE_DATE,
    PRODUCER_BY_CODE,
    WHERE_CREATION_DATE,
    WHERE_CREATION_DATE_FILES,
    WHERE_CREATION_RANGE,
    WHERE_EMISSION_DATE,
    WHERE_EMISSION_RANGE,
    WHERE_NO_FILTER,
    build_insureds_query,
    build_mark_sent_query,
    build_paginated_query,
    build_producers_query,
    map_insured,
    map_policies,
    map_producer,
)
from app.schemas import InsuredResponse, Policy, ProducerResponse
from app.sql_client import SQLServerClient

logger = get_logger(__name__)


class PolicyRepository:
    """Read-only repository for policy data from SQL Server.

    Thin orchestrator:  query -> execute -> map -> return domain objects.
    Zero SQL, zero mapping logic here -- all in queries.py.
    """

    def __init__(self, sql_client: SQLServerClient):
        self.sql_client = sql_client
        self._local_prefix = settings.local_path_prefix

    # =========================================================================
    # Pagination helper
    # =========================================================================

    async def _get_paginated(
        self,
        where_clause: str,
        params: list,
        limit: int = 500,
        offset: int = 0,
        order_dir: str = "DESC",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Execute a paginated policy query."""
        query = build_paginated_query(where_clause, order_dir, sort_by)
        rows = await self.sql_client.execute_query(query, params + [offset, offset + limit])
        return map_policies(rows, self._local_prefix)

    # =========================================================================
    # Policy queries
    # =========================================================================

    async def get_all(
        self,
        limit: int = 100,
        offset: int = 0,
        order_dir: str = "DESC",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """List all policies with pagination (no date filter)."""
        return await self._get_paginated(WHERE_NO_FILTER, [], limit, offset, order_dir, sort_by)

    async def get_by_number(self, nro_pol: str) -> Policy:
        """Get complete policy by number (with all endorsements and files)."""
        rows = await self.sql_client.execute_query(POLICY_BY_NUMBER, [nro_pol])
        if not rows:
            raise EntityNotFoundError("Policy", nro_pol)
        return map_policies(rows, self._local_prefix)[0]

    async def get_by_emission_date(
        self,
        emission_date: date,
        limit: int = 500,
        offset: int = 0,
        order_dir: str = "DESC",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by emission date."""
        return await self._get_paginated(
            WHERE_EMISSION_DATE,
            [emission_date.strftime("%Y-%m-%d")],
            limit,
            offset,
            order_dir,
            sort_by,
        )

    async def count_by_emission_date(self, emission_date: date) -> int:
        """Count distinct policies emitted on a date — direct pv_header query.

        Does NOT join timpresion_pdf, so it always returns the true count of
        policies that exist for that date regardless of whether PDFs exist yet.
        Use this as the stable baseline for readiness checks.
        """
        rows = await self.sql_client.execute_query(
            COUNT_EMITTED_BY_DATE, [emission_date.strftime("%Y-%m-%d")]
        )
        return int(rows[0]["total"]) if rows else 0

    async def get_no_files_by_emission_date(self, emission_date: date) -> list[dict]:
        """Return policies emitted on a date that have NO entry in timpresion_pdf.

        These are policies whose PDFs haven't been generated yet by the backend.
        Each row has: nro_pol, ramo, cod_aseg, nombre_asegurado, cod_agente, nombre_agente.
        """
        return await self.sql_client.execute_query(
            POLICIES_NO_FILES_BY_DATE, [emission_date.strftime("%Y-%m-%d")]
        )

    async def get_by_creation_date(
        self,
        creation_date: date,
        limit: int = 500,
        offset: int = 0,
        order_dir: str = "DESC",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by PDF file creation date."""
        return await self._get_paginated(
            WHERE_CREATION_DATE,
            [creation_date.strftime("%Y-%m-%d")],
            limit,
            offset,
            order_dir,
            sort_by,
        )

    async def get_by_date_range(
        self,
        start_date: date,
        end_date: date,
        limit: int = 500,
        offset: int = 0,
        order_dir: str = "DESC",
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies by emission date range."""
        return await self._get_paginated(
            WHERE_EMISSION_RANGE,
            [start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")],
            limit,
            offset,
            order_dir,
            sort_by,
        )

    async def get_with_files(
        self,
        start_date: date,
        end_date: date | None = None,
        limit: int = 500,
        offset: int = 0,
        sort_by: str = "emission_date",
    ) -> list[Policy]:
        """Get policies that have files (ready for sending)."""
        if end_date:
            return await self._get_paginated(
                WHERE_CREATION_RANGE,
                [start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")],
                limit,
                offset,
                sort_by=sort_by,
            )
        return await self._get_paginated(
            WHERE_CREATION_DATE_FILES,
            [start_date.strftime("%Y-%m-%d")],
            limit,
            offset,
            sort_by=sort_by,
        )

    async def get_since(self, since: date) -> list[Policy]:
        """Get policies with files created since a date (for cache sync)."""
        logger.info("Loading policies since %s...", since.isoformat())
        rows = await self.sql_client.execute_query(POLICY_SINCE_DATE, [since.strftime("%Y-%m-%d")])
        policies = map_policies(rows, self._local_prefix)
        logger.info("Loaded %s policies since %s", len(policies), since.isoformat())
        return policies

    # =========================================================================
    # Producers
    # =========================================================================

    async def get_producer(self, code: str) -> ProducerResponse:
        """Get producer by code."""
        rows = await self.sql_client.execute_query(PRODUCER_BY_CODE, [code])
        if not rows:
            raise EntityNotFoundError("Producer", code)
        return ProducerResponse(**map_producer(rows[0]))

    async def get_producers_list(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        order_dir: str = "ASC",
    ) -> list[ProducerResponse]:
        """List producers with pagination and optional name/email search."""
        sql, extra_params = build_producers_query(search, order_dir)
        rows = await self.sql_client.execute_query(sql, extra_params + [offset, offset + limit])
        return [ProducerResponse(**map_producer(r)) for r in rows]

    # =========================================================================
    # Insureds
    # =========================================================================

    async def get_insured(self, code: str) -> InsuredResponse:
        """Get insured by insured code (cod_aseg)."""
        rows = await self.sql_client.execute_query(INSURED_BY_CODE, [code])
        if not rows:
            raise EntityNotFoundError("Insured", code)
        return InsuredResponse(**map_insured(rows[0]))

    async def get_insureds_list(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        order_dir: str = "ASC",
    ) -> list[InsuredResponse]:
        """List insureds with pagination and optional name/email search."""
        sql, extra_params = build_insureds_query(search, order_dir)
        rows = await self.sql_client.execute_query(sql, extra_params + [offset, offset + limit])
        return [InsuredResponse(**map_insured(r)) for r in rows]

    # =========================================================================
    # Write operations
    # =========================================================================

    async def mark_sent(self, nro_pol_list: list[str], sent_date: date) -> int:
        """Mark timpresion_pdf rows as sent for the given policy numbers.

        Sets sn_envio = 1, fec_envio = sent_date for all PDF rows belonging
        to the given policies (only rows not already marked).

        Returns the number of updated rows.
        """
        if not nro_pol_list:
            return 0
        sql = build_mark_sent_query(len(nro_pol_list))
        sent_datetime = datetime.combine(sent_date, datetime.min.time())
        params = [sent_datetime] + list(nro_pol_list)
        updated = await self.sql_client.execute_command(sql, params)
        logger.info(
            "mark_sent: updated %d rows for %d policies on %s",
            updated,
            len(nro_pol_list),
            sent_date.isoformat(),
        )
        return updated
