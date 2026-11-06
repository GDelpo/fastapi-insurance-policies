"""Tests for cache management endpoints."""


class TestCacheStats:
    """GET /api/v1/cache/stats."""

    def test_returns_stats(self, client):
        resp = client.get("/api/v1/cache/stats")
        assert resp.status_code == 200
        assert "total_policies" in resp.json()


class TestCacheRefresh:
    """POST /api/v1/cache/refresh."""

    def test_refresh_default_days(self, client, mock_service):
        resp = client.post("/api/v1/cache/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "30" in data["message"]

    def test_refresh_custom_days(self, client, mock_service):
        resp = client.post("/api/v1/cache/refresh?days=7")
        assert resp.status_code == 200
        assert "7" in resp.json()["message"]


class TestCacheWarmup:
    """POST /api/v1/cache/warmup."""

    def test_warmup_returns_response(self, client):
        resp = client.post("/api/v1/cache/warmup?days=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "policiesCached" in data
        assert "durationSeconds" in data


class TestCacheInvalidation:
    """DELETE endpoints for cache invalidation."""

    def test_invalidate_policy(self, client, mock_service):
        resp = client.delete("/api/v1/cache/policies/POL-001")
        assert resp.status_code == 200
        assert "POL-001" in resp.json()["message"]
        mock_service.invalidate_policy_cache.assert_called_once_with("POL-001")

    def test_invalidate_date(self, client, mock_service):
        resp = client.delete("/api/v1/cache/date/2025-06-18")
        assert resp.status_code == 200
        mock_service.invalidate_date_cache.assert_called_once()

    def test_clear_all(self, client, mock_service):
        resp = client.delete("/api/v1/cache/clear")
        assert resp.status_code == 200
        assert "5" in resp.json()["message"]
        mock_service.clear_cache.assert_called_once()
