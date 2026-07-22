from integration_hub import integration_status


def test_integration_status_never_exposes_secrets(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "super-secret")
    status = integration_status()
    encoded = str(status)
    assert "super-secret" not in encoded
    providers = {p["key"]: p for p in status["providers"]}
    assert providers["the-odds-api"]["configured"] is True
    assert "odds" in providers["the-odds-api"]["datasets"]


def test_integration_status_route(client):
    response = client.get("/api/admin/integrations")
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"]["total"] >= 5
    assert any(p["key"] == "nflverse" for p in body["providers"])


def test_integration_sync_requires_season(client):
    response = client.post("/api/admin/integrations/sync", json={"datasets": ["weather"]})
    assert response.status_code == 400
