"""GET /data/status shows how this server gets prices (no secrets), so a deploy can be checked from outside."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_status_shows_the_price_mode_and_the_guard_settings(monkeypatch):
    monkeypatch.setenv("EGOQUANT_PRICE_MODE", "snapshot")
    monkeypatch.delenv("EGOQUANT_PORTFOLIO_METHOD", raising=False)
    data = client.get("/data/status").json()
    assert data["price_mode"] == "snapshot" and data["portfolio_method"] == "v1"
    assert data["live_budget_seconds"] > 0 and data["slow_request_seconds"] > 0
    assert data["python_version"].count(".") == 2          # the Python the server really runs (3.14.x on Railway)


def test_status_default_is_live(monkeypatch):
    monkeypatch.delenv("EGOQUANT_PRICE_MODE", raising=False)
    assert client.get("/data/status").json()["price_mode"] == "live"


def test_status_shows_the_risk_game_settings_as_applied(monkeypatch):
    monkeypatch.delenv("EGOQUANT_RISK_GAME_WEIGHT", raising=False)
    monkeypatch.delenv("EGOQUANT_RISK_GAME_MAY_RAISE", raising=False)
    data = client.get("/data/status").json()
    assert abs(data["risk_game_weight"] - 1 / 3) < 1e-6 and data["risk_game_may_raise"] is True
    monkeypatch.setenv("EGOQUANT_RISK_GAME_WEIGHT", "0")
    monkeypatch.setenv("EGOQUANT_RISK_GAME_MAY_RAISE", "off")
    data = client.get("/data/status").json()
    assert data["risk_game_weight"] == 0.0 and data["risk_game_may_raise"] is False
