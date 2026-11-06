"""Pytest fixtures for the policy service tests.

Auth: overrides get_current_user, service_role_required, admin_role_required
      so tests don't need a real identity service.
Service: mocks PolicyService as an AsyncMock stored in app.state.
All names, emails and codes below are fictitious.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import TokenData
from app.schemas import (
    CacheWarmupResponse,
    InsuredResponse,
    Policy,
    ProducerResponse,
    ReadinessResponse,
)

# =========================================================================
# Auth fixtures
# =========================================================================


def _make_token_data(role: str = "service") -> TokenData:
    return TokenData(id=uuid4(), user_name="test-user", mail="test-user@example.com", role=role)


@pytest.fixture()
def service_user() -> TokenData:
    return _make_token_data("service")


@pytest.fixture()
def admin_user() -> TokenData:
    return _make_token_data("admin")


# =========================================================================
# Domain fixtures
# =========================================================================


@pytest.fixture()
def sample_policy() -> Policy:
    """Minimal valid Policy for testing."""
    from app.schemas import Endorsement, Insured, PolicyFile, Producer

    return Policy(
        nro_pol="POL-001",
        ramo="Automotores",
        emission_date=date(2025, 6, 18),
        insured=Insured(code="12345", name="Juan Pérez", email="juan.perez@example.com"),
        producer=Producer(code="100", name="ACME Brokers", email="acme@example.com"),
        endorsements=[
            Endorsement(
                nro_endoso=0,
                emission_date=date(2025, 6, 18),
                files=[
                    PolicyFile(
                        name="POL-001_TOASEG.pdf",
                        file_type="TOASEG",
                        original_path="\\\\fileserver\\policies\\POL-001_TOASEG.pdf",
                        local_path="/media/polizas/POL-001_TOASEG.pdf",
                        available=True,
                    )
                ],
            )
        ],
    )


@pytest.fixture()
def sample_policy_no_files() -> Policy:
    """Policy without files (missing)."""
    from app.schemas import Endorsement, Insured, Producer

    return Policy(
        nro_pol="POL-002",
        ramo="Vida",
        emission_date=date(2025, 6, 18),
        insured=Insured(code="99999", name="María Gómez"),
        producer=Producer(code="200", name="Example Brokers"),
        endorsements=[Endorsement(nro_endoso=0, emission_date=date(2025, 6, 18), files=[])],
    )


@pytest.fixture()
def sample_producer() -> ProducerResponse:
    return ProducerResponse(code="100", name="ACME Brokers", email="acme@example.com")


@pytest.fixture()
def sample_insured() -> InsuredResponse:
    return InsuredResponse(code="12345", name="Juan Pérez", email="juan.perez@example.com")


# =========================================================================
# Mock PolicyService
# =========================================================================


@pytest.fixture()
def mock_service(sample_policy):
    """AsyncMock PolicyService with sensible defaults."""
    svc = AsyncMock()
    svc.list_policies = AsyncMock(return_value=([sample_policy], 1))
    svc.get_policy = AsyncMock(return_value=sample_policy)
    svc.check_readiness = AsyncMock(
        return_value=ReadinessResponse(
            date=date(2025, 6, 18),
            total=1,
            complete=1,
            missing=0,
            in_storage=1,
            not_in_storage=0,
            ready=True,
        )
    )
    svc.get_producers = AsyncMock(return_value=[])
    svc.get_producer = AsyncMock(return_value=ProducerResponse(code="100", name="Prod", email=None))
    svc.get_insureds = AsyncMock(return_value=[])
    svc.get_insured = AsyncMock(return_value=InsuredResponse(code="12345", name="Juan"))
    svc.get_cache_stats = AsyncMock(return_value={"total_policies": 100})
    svc.refresh_cache = AsyncMock(return_value={"total_policies": 100})
    svc.warmup_cache = AsyncMock(
        return_value=CacheWarmupResponse(success=True, policies_cached=50, duration_seconds=1.2)
    )
    svc.clear_cache = AsyncMock(return_value=5)
    svc.invalidate_policy_cache = AsyncMock()
    svc.invalidate_date_cache = AsyncMock()
    return svc


# =========================================================================
# Test client factory
# =========================================================================


@pytest.fixture()
def client(mock_service, service_user) -> TestClient:
    """TestClient with auth overridden (service role) and mocked service.

    No `with` block: the real lifespan (SQL Server + Redis) never runs.
    """
    from app.auth import admin_role_required, get_current_user, service_role_required
    from app.dependencies import get_policy_service
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: service_user
    app.dependency_overrides[service_role_required] = lambda: service_user
    app.dependency_overrides[admin_role_required] = lambda: service_user
    app.dependency_overrides[get_policy_service] = lambda: mock_service

    # Minimal app.state stubs for the health check
    app.state.sql_client = MagicMock()
    app.state.sql_client.test_connection.return_value = True
    app.state.cache_manager = MagicMock()
    app.state.cache_manager.redis.ping = AsyncMock(return_value=True)
    app.state.cache_manager.get_all_count = AsyncMock(return_value=1)
    app.state.policy_service = mock_service

    yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()
