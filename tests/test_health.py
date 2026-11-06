"""Tests for the health check endpoint."""


class TestHealthCheck:
    """Health check lives at /health (outside api_prefix)."""

    def test_healthy_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "environment" in data
        assert isinstance(data["dependencies"], dict)
        assert len(data["dependencies"]) == 2

    def test_dependencies_include_sqlserver_and_redis(self, client):
        resp = client.get("/health")
        assert set(resp.json()["dependencies"]) == {"sqlserver", "redis"}

    def test_unhealthy_db_returns_503(self, client):
        """When SQL Server is down, health returns 503."""
        from app.main import app

        app.state.sql_client.test_connection.return_value = False
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unhealthy"
