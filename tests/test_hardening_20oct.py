"""Hardening found by Damla's hostile-user pass (20 Oct): no redirect to http, HSTS, Retry-After, a debug route that does not
exist, a malformed token that is a 401, and 'no portfolio' that is not 'rejected by the Risk Engine'."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from egoquant.accounts.store import AccountError, SupabaseStore
from egoquant.agents.quant.agent import AgentConfig
from egoquant.report import _banners
from egoquant.risk_engine import RiskReview

client = TestClient(main.app)


def test_a_trailing_slash_is_a_404_not_a_redirect_to_http():
    assert client.get("/health").status_code == 200
    r = client.get("/health/", follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers


def test_the_procfile_trusts_the_proxy_headers():
    text = (Path(__file__).parents[1] / "Procfile").read_text(encoding="utf-8")
    assert "--proxy-headers" in text and "--forwarded-allow-ips" in text


def test_hsts_is_on_every_answer_including_errors():
    for r in (client.get("/health"), client.get("/nope"), client.post("/profile", json={})):
        assert r.headers["strict-transport-security"].startswith("max-age=")


def test_a_429_says_when_to_come_back(monkeypatch):
    monkeypatch.setattr(main, "_chat_timestamps", __import__("collections").deque([time.monotonic()] * main._CHAT_LIMIT))
    r = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_the_debug_page_does_not_exist_whatever_the_body_when_it_is_off(monkeypatch):
    monkeypatch.delenv("EGOQUANT_DEBUG_PAGES", raising=False)
    for body in ({}, {"c_level": "C9"}, {"c_level": "C3"}, {"junk": 1}):
        assert client.post("/debug/portfolio", json=body).status_code == 404
    monkeypatch.setenv("EGOQUANT_DEBUG_PAGES", "on")
    assert client.post("/debug/portfolio", json={"c_level": "C9"}).status_code == 422       # on: the body is checked as before


def test_a_token_the_auth_service_refuses_is_a_401_not_a_403():
    class Session:
        def request(self, method, url, **kw):
            return SimpleNamespace(status_code=403, content=b"x", json=lambda: {})
    store = SupabaseStore("https://example.supabase.co", "anon-key", session=Session())
    with pytest.raises(AccountError) as error:
        store.user("garbage")
    assert error.value.status == 401


def test_no_portfolio_is_not_reported_as_rejected_by_the_risk_engine():
    nothing = RiskReview(approved=False, violations=["proposal has no holdings (status infeasible)"], checks=[])
    banners = _banners("historical", AgentConfig(), nothing)
    assert banners[0].startswith("NO PORTFOLIO") and "NOT APPROVED" not in " ".join(banners)
    rejected = RiskReview(approved=False, violations=["equity weight 85.61% vs limit 70.00% (suitability:max_equity)"], checks=[])
    text = _banners("historical", AgentConfig(), rejected)[0]
    assert text.startswith("NOT APPROVED") and "your share in stocks would be 85.6%" in text and "suitability" not in text
