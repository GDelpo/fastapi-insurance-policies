"""Tests for producer and insured endpoints."""

from unittest.mock import AsyncMock

from app.exceptions import EntityNotFoundError


class TestProducers:
    """Producer endpoints require admin role."""

    def test_list_producers(self, client, mock_service, sample_producer):
        mock_service.get_producers = AsyncMock(return_value=[sample_producer])
        resp = client.get("/api/v1/producers")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "meta" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["code"] == "100"

    def test_get_producer_by_code(self, client, mock_service, sample_producer):
        mock_service.get_producer = AsyncMock(return_value=sample_producer)
        resp = client.get("/api/v1/producers/100")
        assert resp.status_code == 200
        assert resp.json()["code"] == "100"

    def test_producer_not_found(self, client, mock_service):
        mock_service.get_producer = AsyncMock(side_effect=EntityNotFoundError("Producer", "999"))
        resp = client.get("/api/v1/producers/999")
        assert resp.status_code == 404

    def test_producers_search_param(self, client, mock_service):
        client.get("/api/v1/producers?search=abc")
        mock_service.get_producers.assert_called_once()
        call_kwargs = mock_service.get_producers.call_args
        assert call_kwargs.kwargs.get("search") == "abc" or "abc" in str(call_kwargs)


class TestInsureds:
    """Insured endpoints require admin role."""

    def test_list_insureds(self, client, mock_service, sample_insured):
        mock_service.get_insureds = AsyncMock(return_value=[sample_insured])
        resp = client.get("/api/v1/insureds")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["code"] == "12345"

    def test_get_insured_by_code(self, client, mock_service, sample_insured):
        mock_service.get_insured = AsyncMock(return_value=sample_insured)
        resp = client.get("/api/v1/insureds/12345")
        assert resp.status_code == 200
        assert resp.json()["code"] == "12345"

    def test_insured_not_found(self, client, mock_service):
        mock_service.get_insured = AsyncMock(side_effect=EntityNotFoundError("Insured", "00000"))
        resp = client.get("/api/v1/insureds/00000")
        assert resp.status_code == 404
