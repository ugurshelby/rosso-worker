"""Worker health servisi testi — DB gerektirmez (sadece /health shallow)."""
from fastapi.testclient import TestClient

from app.health import app


def test_health_ok():
    with TestClient(app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["service"] == "rosso-worker"
