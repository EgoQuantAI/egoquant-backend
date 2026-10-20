"""The saved-prices note in a REPORT names only the feeds of the instruments priced for that investor (TRACKER F13): a funds-only
universe does not say "crypto from Binance", a universe with crypto pairs does. /portfolio in snapshot price mode (the committed
Yahoo snapshot, no network, no key)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from tests.system.test_risk_game_blend import questionnaire_side, random_answers


@pytest.fixture(scope="module")
def client():
    from main import app
    keys = ("EGOQUANT_PRICE_MODE", "EGOQUANT_BRIEFING", "EGOQUANT_NEWS_WORKER", "EGOQUANT_WARM", "EGOQUANT_DATA", "TIINGO_API_KEY")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"EGOQUANT_PRICE_MODE": "snapshot", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
    for key in ("EGOQUANT_DATA", "TIINGO_API_KEY"):
        os.environ.pop(key, None)
    yield TestClient(app, raise_server_exceptions=False)
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def build(client, cls, areas, seed=None):
    answers = random_answers(seed if seed is not None else next(s for s in range(9400, 9900) if questionnaire_side(random_answers(s))[3] == cls))
    egoquant_routes._portfolio_timestamps.clear()
    r = client.post("/portfolio", json={"answers": answers, "interest_areas": areas, "horizon_years": 10.0})
    assert r.status_code == 200, r.text[:200]
    return r.json()


def test_a_funds_only_portfolio_does_not_credit_binance(client):
    out = build(client, "C3", ["stocks", "bonds"])
    assert out["snapshot_first"] and out["price_note"].startswith("saved snapshot of real prices as of")
    assert "Binance" not in out["price_note"] and "crypto" not in out["price_note"], out["price_note"]
    banner = next(b for b in out["report"]["banners"] if b.startswith("Prices:"))
    assert "Binance" not in banner and "Yahoo Finance" in banner, banner
    assert not any("Binance" in str(v) for v in (out["price_note"], out["report"].get("price_note")))


def test_a_portfolio_with_crypto_in_its_universe_credits_binance(client):
    out = build(client, "C5", ["crypto", "stocks"], seed=9395)                       # a C5 whose answers allow crypto (K4)
    crypto_in_universe = [i["ticker"] for i in out["universe"]["instruments"] if i["asset_class"] == "crypto" and i["ticker"].endswith("USDT")]
    assert crypto_in_universe, "this C5 investor should have Binance crypto pairs in the universe"
    assert "crypto from Binance" in out["price_note"], out["price_note"]
    banner = next(b for b in out["report"]["banners"] if b.startswith("Prices:"))
    assert "crypto from Binance" in banner, banner


def test_a_growth_investor_who_cannot_hold_crypto_is_not_told_the_prices_come_partly_from_binance(client):
    """The case behind F13: C4/C5 investors whose answers block crypto asked for crypto, got none, and the note still said Binance."""
    out = build(client, "C4", ["crypto", "stocks"], seed=9342)
    assert not [i for i in out["universe"]["instruments"] if i["asset_class"] == "crypto"]
    assert "Binance" not in out["price_note"], out["price_note"]
