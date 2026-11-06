"""Tests for policy API endpoints."""

from unittest.mock import AsyncMock

from app.exceptions import EntityNotFoundError


class TestListPolicies:
    """GET /api/v1/policies — paginated policy listing."""

    def test_returns_paginated_response(self, client):
        resp = client.get("/api/v1/policies?emission_date=2025-06-18")
        assert resp.status_code == 200
        data = resp.json()
        assert data["meta"]["total"] == 1
        assert len(data["data"]) == 1
        assert data["data"][0]["nroPol"] == "POL-001"

    def test_pagination_meta_fields(self, client):
        resp = client.get("/api/v1/policies?emission_date=2025-06-18")
        meta = resp.json()["meta"]
        assert set(meta) == {"total", "page", "pageSize", "totalPages", "hasNext", "hasPrevious"}

    def test_default_pagination_values(self, client):
        meta = client.get("/api/v1/policies?emission_date=2025-06-18").json()["meta"]
        assert meta["pageSize"] == 20
        assert meta["page"] == 1
        assert meta["hasPrevious"] is False

    def test_custom_pagination(self, client, mock_service):
        resp = client.get("/api/v1/policies?emission_date=2025-06-18&limit=10&skip=10")
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert meta["pageSize"] == 10
        assert meta["page"] == 2
        pagination = mock_service.list_policies.call_args.kwargs["pagination"]
        assert (pagination.limit, pagination.skip) == (10, 10)

    def test_no_filter_calls_list_policies(self, client, mock_service):
        resp = client.get("/api/v1/policies")
        assert resp.status_code == 200
        mock_service.list_policies.assert_called_once()

    def test_sort_order_accepts_asc(self, client):
        assert client.get("/api/v1/policies?sort_order=asc").status_code == 200

    def test_invalid_sort_order_returns_422(self, client):
        assert client.get("/api/v1/policies?sort_order=invalid").status_code == 422


class TestGetPolicy:
    """GET /api/v1/policies/{nro_pol} — single policy."""

    def test_returns_policy(self, client):
        resp = client.get("/api/v1/policies/POL-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nroPol"] == "POL-001"
        assert "insured" in data
        assert "producer" in data
        assert "endorsements" in data

    def test_computed_fields_present(self, client):
        data = client.get("/api/v1/policies/POL-001").json()
        for field in ("endorsementCount", "totalFileCount", "hasFiles", "allInStorage"):
            assert field in data

    def test_not_found_returns_404(self, client, mock_service):
        mock_service.get_policy = AsyncMock(side_effect=EntityNotFoundError("Policy", "MISSING"))
        resp = client.get("/api/v1/policies/MISSING")
        assert resp.status_code == 404
        data = resp.json()
        assert data["success"] is False
        assert "not found" in data["message"].lower()

    def test_storage_fields_present_and_internal_ones_hidden(self, client):
        file_data = client.get("/api/v1/policies/POL-001").json()["endorsements"][0]["files"][0]
        assert "inStorage" in file_data
        assert "storageLink" in file_data
        # original_path (network share) and storage_id are excluded from responses
        assert "originalPath" not in file_data
        assert "storageId" not in file_data


class TestReadiness:
    """GET /api/v1/policies/readiness — readiness check."""

    def test_readiness_requires_emission_date(self, client):
        assert client.get("/api/v1/policies/readiness").status_code == 422

    def test_readiness_returns_status(self, client):
        resp = client.get("/api/v1/policies/readiness?emission_date=2025-06-18")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["total"] == 1
        assert data["complete"] == 1
        assert data["missing"] == 0
        assert data["inStorage"] == 1
        assert data["notInStorage"] == 0

    def test_readiness_before_policy_path(self, client):
        """/policies/readiness is not captured by /policies/{nro_pol}."""
        resp = client.get("/api/v1/policies/readiness?emission_date=2025-06-18")
        assert resp.status_code == 200
        assert "ready" in resp.json()


class TestStorageMetadata:
    """PATCH/DELETE /api/v1/policies/{id}/files/{file_id}/storage."""

    URL = "/api/v1/policies/POL-001/files/POL-001_TOASEG.pdf/storage"

    def test_update_storage_metadata(self, client, mock_service):
        from app.schemas import StorageMetadataResponse

        mock_service.save_storage_metadata = AsyncMock(
            return_value=StorageMetadataResponse(
                policy_id="POL-001",
                file_id="POL-001_TOASEG.pdf",
                storage_id="obj_abc123",
                storage_link="https://storage.example.com/bucket/POL-001_TOASEG.pdf",
            )
        )
        resp = client.patch(
            self.URL,
            json={
                "storageId": "obj_abc123",
                "storageLink": "https://storage.example.com/bucket/POL-001_TOASEG.pdf",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["policyId"] == "POL-001"
        assert data["storageId"] == "obj_abc123"
        assert data["saved"] is True

    def test_update_storage_metadata_missing_fields(self, client):
        assert client.patch(self.URL, json={}).status_code == 422

    def test_delete_storage_metadata(self, client, mock_service):
        mock_service.delete_storage_metadata = AsyncMock(return_value=True)
        resp = client.delete(self.URL)
        assert resp.status_code == 200
        assert "deleted" in resp.json()["message"].lower()

    def test_delete_storage_metadata_not_found(self, client, mock_service):
        mock_service.delete_storage_metadata = AsyncMock(return_value=False)
        resp = client.delete("/api/v1/policies/POL-001/files/MISSING.pdf/storage")
        assert resp.status_code == 200
        assert "no storage metadata" in resp.json()["message"].lower()


class TestMarkSent:
    """POST /api/v1/policies/mark-sent."""

    def test_mark_sent(self, client, mock_service):
        mock_service.mark_sent = AsyncMock(return_value=4)
        resp = client.post(
            "/api/v1/policies/mark-sent",
            json={"nroPolList": ["POL-001", "POL-002"], "sentDate": "2025-06-18"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"updatedRows": 4, "policyCount": 2}

    def test_mark_sent_rejects_empty_list(self, client):
        resp = client.post(
            "/api/v1/policies/mark-sent", json={"nroPolList": [], "sentDate": "2025-06-18"}
        )
        assert resp.status_code == 422
