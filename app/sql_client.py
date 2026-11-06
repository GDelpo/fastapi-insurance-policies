"""SQL Server client for read-only access to production database."""

import asyncio
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import pyodbc

from app.config import settings
from app.exceptions import DatabaseError, ServiceException, SQLServerConnectionError
from app.logger import get_logger

logger = get_logger(__name__)

# Enable ODBC driver-level connection pooling (must be set before first connect)
pyodbc.pooling = True

# Thread pool for running sync pyodbc calls.
# 10 workers matches typical FastAPI concurrency for read-heavy API usage.
_executor = ThreadPoolExecutor(max_workers=10)


class SQLServerClient:
    """Client for SQL Server operations."""

    def __init__(self):
        """Initialize SQL Server client."""
        self._connection_string = settings.sqlserver_connection_string
        self._connected = False

    def test_connection(self) -> bool:
        """Test database connection."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                self._connected = True
                logger.info("✅ SQL Server connection successful")
                return True
        except Exception as e:
            logger.warning("⚠️ SQL Server connection failed: %s", e)
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        """Check if last connection test was successful."""
        return self._connected

    @contextmanager
    def _get_connection(self) -> Generator[pyodbc.Connection, None, None]:
        """Get a database connection (pooled by ODBC driver manager)."""
        conn = None
        try:
            conn = pyodbc.connect(self._connection_string, autocommit=True)
            yield conn
        except pyodbc.Error as e:
            logger.error("SQL Server connection error: %s", e)
            raise SQLServerConnectionError(str(e)) from e
        finally:
            if conn:
                conn.close()

    def _execute_query(
        self,
        query: str,
        params: list[Any] | None = None,
        _retries: int = 2,
    ) -> list[dict]:
        """Execute a query and return results as list of dicts (sync).

        Retries up to _retries times on connection errors (transient SQL Server
        overload or stale pooled connections). Each attempt opens a fresh
        connection so a dead pooled connection doesn't block the caller.
        """
        import time as _time

        last_exc: Exception | None = None
        for attempt in range(1, _retries + 2):  # _retries+1 total attempts
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    columns = [column[0] for column in cursor.description]
                    rows = cursor.fetchall()
                    return [dict(zip(columns, row, strict=False)) for row in rows]
            except (pyodbc.Error, ServiceException) as e:
                # ServiceException covers SQLServerConnectionError from _get_connection()
                last_exc = e
                if attempt <= _retries:
                    logger.warning(
                        "Query attempt %d/%d failed, retrying in 1s: %s",
                        attempt,
                        _retries + 1,
                        e,
                    )
                    _time.sleep(1)
        logger.error("Query failed after %d attempts: %s", _retries + 1, last_exc)
        if isinstance(last_exc, SQLServerConnectionError):
            raise last_exc
        raise DatabaseError("query", str(last_exc)) from last_exc

    async def execute_query(
        self,
        query: str,
        params: list[Any] | None = None,
    ) -> list[dict]:
        """Execute a query asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            self._execute_query,
            query,
            params,
        )

    def _execute_command(
        self,
        query: str,
        params: list[Any] | None = None,
    ) -> int:
        """Execute a DML statement (UPDATE/INSERT/DELETE) and return rowcount (sync)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor.rowcount

    async def execute_command(
        self,
        query: str,
        params: list[Any] | None = None,
    ) -> int:
        """Execute a DML statement asynchronously. Returns number of affected rows."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            self._execute_command,
            query,
            params,
        )
