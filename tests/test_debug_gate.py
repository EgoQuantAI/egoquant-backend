"""/debug/portfolio exists only with EGOQUANT_DEBUG_PAGES=on; unset (production) it is a 404 that spends no rate limit."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import egoquant_routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "off")
    egoquant_routes._debug_timestamps.clear()
    app = FastAPI()
    app.include_router(egoquant_routes.router)
    return TestClient(app)


@pytest.mark.parametrize("value", [None, "", "off", "0", "false", "no", "maybe"])
def test_debug_portfolio_is_404_unless_the_flag_is_on(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("EGOQUANT_DEBUG_PAGES", raising=False)
    else:
        monkeypatch.setenv("EGOQUANT_DEBUG_PAGES", value)
    r = client.post("/debug/portfolio", json={"c_level": "C3"})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert len(egoquant_routes._debug_timestamps) == 0            # refused before the rate limit


@pytest.mark.parametrize("value", ["on", "1", "true", "YES"])
def test_debug_portfolio_runs_with_the_flag_on(client, monkeypatch, value):
    monkeypatch.setenv("EGOQUANT_DEBUG_PAGES", value)
    r = client.post("/debug/portfolio", json={"c_level": "C3"})
    assert r.status_code == 200 and "chat_context" in r.json()


def test_the_route_stays_out_of_the_openapi_schema():
    assert all(getattr(r, "path", "") != "/debug/portfolio" or not r.include_in_schema for r in egoquant_routes.router.routes)
