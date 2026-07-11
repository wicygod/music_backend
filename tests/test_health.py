def test_health_is_public_for_service_monitoring(monkeypatch) -> None:
    monkeypatch.setenv("MUSIC_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MUSIC_APP_AUTH_TOKEN", "test-app-token")
    monkeypatch.setenv("MUSIC_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("MUSIC_JWT_SECRET", "test-jwt-secret")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/health")
        protected = client.get("/api/feed/home")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert protected.status_code == 401
