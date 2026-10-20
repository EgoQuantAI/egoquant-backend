"""build_web_briefing: the /portfolio briefing. Offline: every feed request goes to a fake getter, no OpenAI."""

from __future__ import annotations

import copy
import json
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from functools import partial

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.briefing import web
from egoquant.briefing.fetch import fixture_as_of, item_id
from egoquant.briefing.models import DEMO_BANNER, LLMSummary
from egoquant.briefing.web import build_web_briefing
from egoquant.demo.personas import PERSONAS
from egoquant.pipeline import run_pipeline
from tests.briefing.common import AS_OF, FakeBackend, config, interests, proposal

LIVE = {"EGOQUANT_BRIEFING": "live"}
YAHOO_VNQ = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=VNQ&region=US&lang=en-US"
REIT_LINK = "https://news.example.com/reits-office"


def rss(*entries: tuple[str, str, float]) -> bytes:
    """An RSS 2.0 body; entries are (title, link, days before AS_OF)."""
    items = "".join(f"<item><title>{t}</title><link>{link}</link>"
                    f"<pubDate>{format_datetime(AS_OF - timedelta(days=d))}</pubDate></item>"
                    for t, link, d in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


class FakeGet:
    """Feed host stand-in: the Yahoo VNQ feed has two real-estate stories, everything else is empty."""

    def __init__(self, fail: bool = False, slow_hosts: tuple[str, ...] = (), delay: float = 0.0):
        self.fail, self.slow_hosts, self.delay = fail, slow_hosts, delay
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail:
            raise ConnectionError("host down")
        if any(h in url for h in self.slow_hosts):
            time.sleep(self.delay)
        if url == YAHOO_VNQ:
            return rss(("REITs gain as office vacancies fall", REIT_LINK, 1.0),
                       ("Commercial property lenders report record inflows", "https://news.example.com/cre", 2.0))
        return rss()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own cache directory (tone/LLM caches) and an empty in-process feed cache for every test."""
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path))

    def no_openai(timeout):
        raise AssertionError("tests never create a real OpenAI client")
    monkeypatch.setattr(web, "_tone_client", no_openai)
    web.clear_feed_cache()
    yield
    web.clear_feed_cache()


def live(get=None, env=None, **kw):
    kw.setdefault("interests", interests("real_estate"))
    kw.setdefault("proposal", proposal({"SPY": 0.7, "VNQ": 0.3}))
    return build_web_briefing(selection=None, as_of=AS_OF, horizon_years=5, config=config(),
                              env=env or LIVE, get=get or FakeGet(), now=lambda: AS_OF, **kw)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def test_mode_comes_from_the_environment():
    assert web.briefing_mode({}) == "live"
    assert web.briefing_mode({"EGOQUANT_BRIEFING": " Fixture "}) == "fixture"
    assert web.briefing_mode({"EGOQUANT_BRIEFING": "off"}) == "off"
    assert web.briefing_mode({"EGOQUANT_BRIEFING": "bogus"}) == "live"


def test_off_gives_no_briefing_and_no_requests():
    get = FakeGet()
    assert live(get, env={"EGOQUANT_BRIEFING": "off"}) is None
    assert get.urls == []


def test_fixture_mode_is_the_demo_labeled_sample_and_makes_no_requests():
    def no_network(url):
        raise AssertionError("fixture mode must not fetch")

    b = build_web_briefing(interests=interests("real_estate", "technology", "gold"), selection=None,
                           proposal=proposal({"VNQ": 0.4, "QQQ": 0.6}), as_of=date(2026, 10, 18), horizon_years=5,
                           env={"EGOQUANT_BRIEFING": "fixture", "OPENAI_MODEL": "some-other-model"},
                           get=no_network)
    assert b is not None and b.data_source == "fixture" and b.banners[0] == DEMO_BANNER
    assert b.as_of == fixture_as_of()                         # the sample's date, not today's
    assert b.items and b.metadata.n_llm_calls == 0
    assert any(i.summary_kind == "llm" for i in b.items)      # committed summaries, despite another OPENAI_MODEL
    assert web.AI_LABEL in b.labels


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------
def test_live_briefing_from_mocked_feeds():
    get = FakeGet()
    b = live(get)
    assert b is not None and b.data_source == "live" and DEMO_BANNER not in b.banners
    assert {i.item.url for i in b.items} == {REIT_LINK, "https://news.example.com/cre"}
    assert all(i.matched_tickers == ["VNQ"] for i in b.items)
    assert all(i.tone is None for i in b.items)               # no FinBERT on the server, nothing cached
    assert b.metadata.n_tone_unavailable == 2
    assert all(i.summary_kind == "template" for i in b.items) and b.metadata.n_llm_calls == 0
    assert web.AI_LABEL not in b.labels and web.NO_LLM_LABEL in b.labels
    assert YAHOO_VNQ in get.urls


def test_live_fetches_only_the_investors_tickers_and_the_official_releases():
    get = FakeGet()
    live(get)
    cfg = config()
    official = {s.url for s in cfg.live_sources.rss}
    tickers = web.web_tickers(interests("real_estate"), None, {"SPY": 0.7, "VNQ": 0.3})
    assert tickers[:2] == ["SPY", "VNQ"]                     # holdings first, then the real-estate area
    assert official <= set(get.urls)
    assert len(set(get.urls) - official) == len(cfg.live_sources.rss_per_ticker) * len(tickers)


def test_per_ticker_feeds_are_capped():
    many = proposal({t: 0.1 for t in ["SPY", "VNQ", "QQQ", "GLD", "AGG", "VTI", "IAU", "TLT", "SHY", "BIL"]})
    tickers = web.web_tickers(interests("real_estate"), None, dict(many.weights))
    assert len(tickers) == web.MAX_WEB_TICKERS


def test_live_tone_comes_from_the_cache_file(tmp_path):
    cfg = config()
    key = f"{item_id('Yahoo Finance', REIT_LINK)}|{cfg.tone.model}"
    tone = {"positive": 0.7, "negative": 0.1, "neutral": 0.2, "tone": 0.6, "model": cfg.tone.model}
    (tmp_path / "briefing").mkdir()
    (tmp_path / "briefing" / "tone_cache.json").write_text(json.dumps({key: tone}), encoding="utf-8")
    b = live()
    scored = {i.item.url: i.tone for i in b.items}
    assert scored[REIT_LINK] is not None and scored[REIT_LINK].tone == 0.6
    assert scored["https://news.example.com/cre"] is None


def test_live_never_loads_finbert(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("FinBERT must not be loaded by the web briefing")

    monkeypatch.setattr("egoquant.briefing.tone.FinBertScorer.__init__", boom)
    assert live() is not None


def test_every_source_failing_gives_none():
    assert live(FakeGet(fail=True)) is None


def test_total_time_cap_skips_slow_feeds_and_keeps_the_rest():
    get = FakeGet(slow_hosts=("nasdaq.com",), delay=2.0)
    start = time.monotonic()
    b = build_web_briefing(interests=interests("real_estate"), selection=None, proposal=proposal({"VNQ": 1.0}),
                           as_of=AS_OF, horizon_years=5, config=config(), env=LIVE, get=get, now=lambda: AS_OF,
                           total_seconds=0.5)
    assert time.monotonic() - start < 1.5
    assert b is not None and b.items
    assert any(s.startswith("rss Nasdaq") and "TimeoutError" in s for s in b.metadata.sources_skipped)


def test_feeds_are_reused_across_requests():
    get = FakeGet()
    live(get)
    n = len(get.urls)
    assert live(get) is not None
    assert len(get.urls) == n                                 # second request: all from the in-process cache


def test_a_failing_feed_is_not_retried_at_once():
    get = FakeGet(fail=True)
    live(get)
    n = len(get.urls)
    live(get)
    assert len(get.urls) == n


def test_any_error_gives_none(monkeypatch):
    def broken(**kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(web, "build_briefing", broken)
    assert live() is None


def test_inputs_are_only_read():
    inv, prop = interests("real_estate"), proposal({"SPY": 0.7, "VNQ": 0.3})
    before = (copy.deepcopy(inv.model_dump()), copy.deepcopy(prop.model_dump()))
    live(interests=inv, proposal=prop)
    assert (inv.model_dump(), prop.model_dump()) == before


def test_a_date_means_that_day_up_to_now():
    now = datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)
    assert web._as_datetime(date(2026, 10, 18), now) == now
    assert web._as_datetime(date(2026, 10, 16), now).date() == date(2026, 10, 16)
    assert web._as_datetime(datetime(2026, 10, 16, 12), now) == AS_OF


# --------------------------------------------------------------------------
# LLM: off by default, capped when switched on
# --------------------------------------------------------------------------
def test_an_openai_key_alone_does_not_switch_the_llm_on(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(web, "_llm_backend", lambda cfg: fake)
    b = live(env={**LIVE, "OPENAI_API_KEY": "sk-test"})
    assert fake.requests == [] and b.metadata.n_llm_calls == 0


def test_headline_only_feeds_are_never_summarized_even_with_the_llm_on(monkeypatch):
    fake = FakeBackend(LLMSummary(summary="Real estate funds were discussed.", llm_tone=0.1))
    monkeypatch.setattr(web, "_llm_backend", lambda cfg: fake)
    cfg = config(enabled=True)
    b = build_web_briefing(interests=interests("real_estate"), selection=None, proposal=proposal({"VNQ": 1.0}),
                           as_of=AS_OF, horizon_years=5, config=cfg,
                           env={**LIVE, "EGOQUANT_BRIEFING_LLM": "1", "OPENAI_API_KEY": "sk-test"},
                           get=FakeGet(), now=lambda: AS_OF)
    assert fake.requests == [] and b.metadata.n_llm_calls == 0       # per-ticker feeds give headlines only
    assert all(i.summary_kind == "template" for i in b.items) and web.AI_LABEL not in b.labels
    assert cfg.llm.max_llm_calls_per_run == 60                # the caller's config is not changed


# --------------------------------------------------------------------------
# Fund-aware tone on the web path
# --------------------------------------------------------------------------
class ToneClient:
    """responses.create stand-in for the web tone calls: fixed answer, optional delay or error; records requests."""

    def __init__(self, answer="negative", delay=0.0, error=None):
        self.answer, self.delay, self.error, self.calls = answer, delay, error, []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return type("R", (), {"output_text": self.answer})()


LLM_ON = {**LIVE, "EGOQUANT_BRIEFING_LLM": "1", "OPENAI_API_KEY": "sk-test"}


def test_uncached_label_and_no_finbert_means_not_rated_with_the_plain_reason():
    from egoquant.briefing.render import briefing_section_html, method_line
    b = live()                                                # LLM not switched on: cache only
    assert all(i.fund_tones == [] and i.tone_method == "unavailable" for i in b.items)
    assert b.metadata.n_fund_tones_unavailable == 2 and b.metadata.n_fund_tones_llm == 0
    assert all(a.tone is None and a.change is None for a in b.instrument_aggregates + b.area_aggregates)
    assert b.alerts == []                                     # left out of the tone scores and alerts
    assert ("2 not rated (left out of the tone scores and alerts): the full news rating had not finished when this "
            "report was made.") in method_line(b)
    html = briefing_section_html(b)
    assert "not rated" in html and "not yet assessed" not in html and "cache only" not in html


def test_web_header_names_the_methods_actually_used():
    from egoquant.briefing.render import briefing_section_html, models_line
    b = live()
    assert models_line(b) == ("Tone: fund-aware gpt-4.1-mini (0), FinBERT fallback (0), not rated (2: the full news "
                              "rating had not finished when this report was made); summaries: a fixed template "
                              "(headline and source).")
    html = briefing_section_html(b)
    assert "ProsusAI" not in html and "Tone model" not in html and "AI-generated summaries" not in html


def test_web_tone_calls_inside_the_budget_are_used_and_cached(tmp_path):
    client = ToneClient("negative")
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client)
    assert len(client.calls) == 2 and b.metadata.n_tone_llm_calls == 2
    assert all(i.tone_method == "llm_fund_aware" and i.fund_tones[0].label == "negative" for i in b.items)
    for call in client.calls:                                # only the headline and one fund, never investor data
        assert "inv-123" not in str(call) and "0.3" not in call["input"] and call["store"] is False
    cache = json.loads((tmp_path / "briefing" / "fund_tone_cache.json").read_text())
    assert len(cache) == 2
    again = ToneClient("positive")
    b2 = live(env=LLM_ON, tone_client_factory=lambda timeout: again)
    assert again.calls == [] and b2.metadata.n_fund_tones_llm == 2   # second request: from the shared cache


def test_late_web_tone_calls_are_not_awaited():
    client = ToneClient("negative", delay=3.0)
    start = time.monotonic()
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client, total_seconds=1.3)
    assert time.monotonic() - start < 2.5                     # the request does not wait for the slow calls
    assert all(i.tone_method == "unavailable" for i in b.items) and b.alerts == []


def test_no_web_tone_calls_without_enough_budget_left():
    client = ToneClient()
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client, total_seconds=web.WEB_TONE_MIN_SECONDS / 2)
    assert client.calls == [] and b.metadata.n_tone_llm_calls == 0
    assert b.metadata.n_fund_tones_unavailable == 2


def test_web_tone_errors_leave_items_not_yet_assessed():
    client = ToneClient(error=ConnectionError("down"))
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client)
    assert len(client.calls) == 2 and all(i.tone_method == "unavailable" for i in b.items)


def test_web_tone_calls_are_capped_per_request(monkeypatch):
    monkeypatch.setattr(web, "WEB_TONE_CALLS", 1)
    client = ToneClient("neutral")
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client)
    assert len(client.calls) == 1 and b.metadata.n_fund_tones_llm == 1 and b.metadata.n_fund_tones_unavailable == 1


def test_web_briefing_period_is_passed_through():
    assert live(briefing_period="month").briefing_period == "month"
    assert build_web_briefing(interests=interests("real_estate"), selection=None, proposal=None, as_of=AS_OF,
                              horizon_years=5, env={"EGOQUANT_BRIEFING": "fixture"},
                              briefing_period="month").briefing_period == "month"


# --------------------------------------------------------------------------
# The pipeline hook
# --------------------------------------------------------------------------
def test_works_as_the_pipeline_briefing_builder():
    p = PERSONAS["growth_investor"]
    run = run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=["real_estate", "technology"],
                       goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=date(2026, 10, 16),
                       trading_platforms=p.trading_platforms, price_source=SyntheticPriceSource(),
                       briefing_builder=partial(build_web_briefing, env={"EGOQUANT_BRIEFING": "fixture"}))
    assert run.report.market_briefing is not None
    assert run.report.banners[0] == DEMO_BANNER


def test_pairs_left_over_are_labeled_in_the_background_for_the_next_request(monkeypatch):
    monkeypatch.setattr(web, "WEB_TONE_CALLS", 1)
    client = ToneClient("negative")
    env = {**LLM_ON, "EGOQUANT_BRIEFING_TONE_BACKGROUND": "on"}
    b = live(env=env, tone_client_factory=lambda timeout: client)
    assert b.metadata.n_fund_tones_llm == 1 and b.metadata.n_fund_tones_unavailable == 1   # response not delayed
    assert web.wait_for_background_tone(10)
    assert len(client.calls) == 2                             # the left-over pair, after the response
    for call in client.calls:                                # same minimal request: headline and one fund
        assert "inv-123" not in str(call) and call["store"] is False
    again = ToneClient("positive")
    b2 = live(env=env, tone_client_factory=lambda timeout: again)
    assert web.wait_for_background_tone(10)
    assert again.calls == [] and b2.metadata.n_fund_tones_llm == 2 and b2.metadata.n_fund_tones_unavailable == 0


def test_background_tone_respects_the_switches_and_the_hourly_cap(monkeypatch):
    client = ToneClient("neutral")
    b = live(env={**LIVE, "EGOQUANT_BRIEFING_TONE_BACKGROUND": "on"}, tone_client_factory=lambda timeout: client)
    assert web.wait_for_background_tone(10) and client.calls == []          # LLM switch off: nothing at all
    b = live(env=LLM_ON, tone_client_factory=lambda timeout: client, total_seconds=web.WEB_TONE_MIN_SECONDS / 2)
    assert web.wait_for_background_tone(10) and client.calls == []          # background off (conftest default)
    monkeypatch.setattr(web, "BACKGROUND_TONE_PER_HOUR", 0)
    live(env={**LLM_ON, "EGOQUANT_BRIEFING_TONE_BACKGROUND": "on"}, tone_client_factory=lambda timeout: client,
         total_seconds=web.WEB_TONE_MIN_SECONDS / 2)
    assert web.wait_for_background_tone(10) and client.calls == []          # cap reached: dropped, not called
