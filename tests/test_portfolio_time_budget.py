"""/portfolio time budget (19 Oct: a 20-area build answered 502 after 249 s on the deployed server).

- The in-request news briefing is CACHE ONLY (no feed request, no OpenAI call) and has a hard time limit; when it is
  late the portfolio is sent without it (the briefing job fills the News tab).
- At most PORTFOLIO_MAX_INTEREST_AREAS interest areas (the investor's first ones) are used for prices and news, and the
  report says so in one sentence.
- Agent A's numbers and the Risk Engine verdict are never skipped or changed by any of this.
SYNTHETIC prices; no network (any attempt is recorded and fails the test)."""

import socket
import threading
import time
from unittest.mock import patch

import pytest

import egoquant_routes as R
from egoquant.briefing import web
from egoquant.demo.personas import PERSONAS
from tests.test_egoquant_routes import portfolio_body

AREAS = ["us_stocks", "international_stocks", "emerging_markets", "china", "small_caps", "dividend_stocks", "technology",
         "healthcare", "financials", "energy", "consumer", "industrials", "utilities", "real_estate", "government_bonds",
         "long_term_bonds", "corporate_bonds", "inflation_protected", "international_bonds", "gold"]
CRYPTO_AREAS = ["crypto", "altcoins", *AREAS[:18]]


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Synthetic prices, LIVE news mode with the LLM switched on (the slow case), an empty cache, no network."""
    attempts: list = []

    def no_network(self, address, *a, **k):
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    env = {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "live", "EGOQUANT_BRIEFING_LLM": "1",
           "OPENAI_API_KEY": "sk-test-not-a-real-key", "EGOQUANT_CACHE_DIR": str(tmp_path),
           "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off"}
    with patch.dict("os.environ", env):
        web.clear_feed_cache()
        R._portfolio_timestamps.clear()
        yield attempts
    web.clear_feed_cache()


def build(persona: str, areas: list[str]):
    R._portfolio_timestamps.clear()
    return R._build_portfolio(R.PortfolioIn(**portfolio_body(PERSONAS[persona], interest_areas=areas)))


@pytest.mark.parametrize("persona, areas", [("growth_investor", AREAS), ("crypto_trader", CRYPTO_AREAS)])
def test_twenty_interest_areas_answer_fast_with_an_empty_cache_and_no_network(offline, persona, areas):
    start = time.monotonic()
    out = build(persona, areas)
    assert time.monotonic() - start < 30
    assert out.approved is not None and out.report["allocation"]
    assert offline == []                                      # no feed request, no OpenAI call inside the request
    assert any("cover 20 interest areas" in b and f"used {R.PORTFOLIO_MAX_INTEREST_AREAS} of them" in b and "left out" in b
               for b in out.report["banners"])


def test_a_slow_briefing_is_cut_off_and_the_portfolio_still_answers(monkeypatch):
    """Independent of the machine's speed: the briefing blocks until the test releases it, so the portfolio can only have
    been sent without waiting for it if it was sent while the briefing was still blocked."""
    release, finished = threading.Event(), threading.Event()

    def slow(**kwargs):
        release.wait(timeout=20)                             # far beyond the limit: the request must not wait for it
        finished.set()
        return None

    monkeypatch.setattr(R, "build_web_briefing", slow)
    monkeypatch.setattr(R, "PORTFOLIO_BRIEFING_SECONDS", 0.3)
    try:
        out = build("growth_investor", AREAS[:3])
        assert not finished.is_set()                         # the build did not wait for the briefing to finish
        assert out.report["market_briefing"] is None and out.report["allocation"]
        assert out.briefing_context is not None              # the briefing job can still start
    finally:
        release.set()                                        # let the abandoned briefing thread end


def test_the_briefing_is_asked_for_the_cache_only(monkeypatch):
    seen: list = []
    monkeypatch.setattr(R, "build_web_briefing", lambda **kw: seen.append(kw.get("cache_only")))
    build("growth_investor", AREAS[:2])
    assert seen == [True]


def test_the_cap_and_the_budget_never_change_agent_a_or_the_risk_engine(monkeypatch):
    """20 areas, capped to the first N, give exactly the portfolio and verdict of asking for those N directly."""
    capped = build("growth_investor", AREAS)
    direct = build("growth_investor", AREAS[:R.PORTFOLIO_MAX_INTEREST_AREAS])
    assert capped.approved == direct.approved and capped.violations == direct.violations
    assert capped.checks == direct.checks
    assert capped.report["allocation"] == direct.report["allocation"]
    assert not any("You chose" in b for b in direct.report["banners"])


def test_cache_only_feeds_make_no_request_and_use_cached_bodies():
    calls: list = []
    web.clear_feed_cache()
    got = web.fetch_feeds(["https://a.invalid/rss"], lambda url: calls.append(url) or b"<rss/>", network=False)
    assert calls == [] and isinstance(got["https://a.invalid/rss"], LookupError)
    web.fetch_feeds(["https://a.invalid/rss"], lambda url: b"<rss/>")             # e.g. the news worker
    got = web.fetch_feeds(["https://a.invalid/rss"], lambda url: calls.append(url) or b"x", network=False)
    assert got["https://a.invalid/rss"] == b"<rss/>" and calls == []
