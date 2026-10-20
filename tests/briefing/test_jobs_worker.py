"""Briefing jobs (/briefing/start, /briefing/{job}) and the background news worker. Offline: injected feeds,
clock and tone client; no network, no OpenAI."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from email.utils import format_datetime

import pytest

from egoquant.agents.market_intel import InvestorInterests
from egoquant.agents.market_intel.agent import DrawdownAlert
from egoquant.briefing import jobs, web
from egoquant.briefing.worker import NewsWorker, worker_enabled, worker_tickers
from tests.briefing.common import AS_OF, config

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US"


def rss(*entries) -> bytes:
    items = "".join(f"<item><title>{t}</title><link>{link}</link>"
                    f"<pubDate>{format_datetime(AS_OF - timedelta(days=d))}</pubDate></item>" for t, link, d in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


FEEDS = {
    YAHOO.format(t="VNQ"): rss(("REITs fall as office vacancies climb", "https://n.example.com/1", 1),
                               ("Real estate funds see weak demand", "https://n.example.com/2", 2),
                               ("REIT shares slump as rates climb", "https://n.example.com/3", 3)),
    YAHOO.format(t="AGG"): rss(("Aggregate bond index posts a monthly gain", "https://n.example.com/4", 1),
                               ("Core bond funds draw steady inflows", "https://n.example.com/5", 2)),
}


class Get:
    def __init__(self):
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return FEEDS.get(url, rss())


class Client:
    """responses.create stand-in: records the fund of each call; fails the first `fail_first` calls."""

    def __init__(self, answer="negative", fail_first=0, delay=0.0):
        self.answer, self.fail_first, self.delay, self.calls = answer, fail_first, delay, []
        self.responses = self

    def create(self, **kw):
        self.calls.append(kw["input"])
        if self.delay:
            time.sleep(self.delay)
        if len(self.calls) <= self.fail_first:
            raise ConnectionError("flaky")
        return type("R", (), {"output_text": self.answer})()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path))
    web.clear_feed_cache()
    jobs.clear_jobs()
    yield
    web.clear_feed_cache()


LIVE = {"EGOQUANT_BRIEFING": "live", "EGOQUANT_BRIEFING_LLM": "1", "OPENAI_API_KEY": "sk-test",
        "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off"}


def facts(weights=None, areas=("real_estate", "bonds")) -> dict:
    interests = InvestorInterests(investor_id="x", interest_areas=list(areas), max_r_level=4)
    return jobs.context_facts(
        interests=interests, watchlist=[], weights=weights, horizon_years=10,
        drawdowns=[DrawdownAlert(ticker="VNQ", window_days=21, drawdown_from_peak=-0.12, message="")],
        names={"VNQ": "Vanguard Real Estate ETF"}, data_to=AS_OF.date(), synthetic=True, drawdown_threshold=0.10,
        interest_tickers=["VNQ", "AGG"])


def run(f, client, env=None, cfg=None, **kw):
    job = jobs.start_job(f, config=cfg or config(), env=env or LIVE, synchronous=True, get=Get(),
                         tone_client_factory=lambda timeout: client, now=lambda: AS_OF, finbert=False, **kw)
    return job


# ---- sealed context -----------------------------------------------------------------------------------------------
def test_context_round_trip_and_tamper_rejection():
    token = jobs.seal_briefing_context(facts({"VNQ": 0.3, "AGG": 0.7}))
    assert jobs.open_briefing_context(token)["weights"] == {"VNQ": 0.3, "AGG": 0.7}
    body, sig = token.split(".")
    with pytest.raises(jobs.BriefingContextError):
        jobs.open_briefing_context(body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig)
    from egoquant.advisor.context import seal_context
    with pytest.raises(jobs.BriefingContextError):            # a chat context is not a briefing context
        jobs.open_briefing_context(seal_context({"kind": "portfolio"}))


def test_rejected_proposal_seals_no_weights():
    assert facts(None)["weights"] is None


# ---- jobs ---------------------------------------------------------------------------------------------------------
def test_job_rates_every_pair_holdings_first_and_reports_progress():
    client = Client("negative")
    job = run(facts({"VNQ": 0.3, "AGG": 0.7}), client)
    assert job.state == "done" and job.total == 5 and job.rated == 5 and job.left_in_background == 0
    first_two = client.calls[:2]
    assert all("AGG" in c or "VNQ" in c for c in client.calls)
    assert job.result["week"]["briefing_period"] == "week" and job.result["month"]["briefing_period"] == "month"
    vnq = next(a for a in job.result["week"]["instrument_aggregates"] if a["subject"] == "VNQ")
    assert vnq["n_negative"] == 3 and vnq["coverage_status"]["state"] == "tone"
    assert vnq["tone"] == pytest.approx(-vnq["evidence"] / (vnq["evidence"] + 1.5), abs=1e-3)          # damped: all negative
    assert job.result["what_changed"][0].startswith("Vanguard Real Estate ETF (VNQ) closed 12.0%")
    assert job.public()["state"] == "done" and "result" in job.public() and first_two


def test_job_priority_puts_holdings_before_interests():
    client = Client("neutral")
    cfg = config()
    cfg.briefing_job.parallel_labels = 1                  # one call at a time, so the call order is the queue order
    run(facts({"AGG": 1.0}), client, cfg=cfg)
    held = [i for i, c in enumerate(client.calls) if "AGG -" in c]
    others = [i for i, c in enumerate(client.calls) if "AGG -" not in c]
    assert held and others and max(held) < min(others)


def test_job_retries_flaky_calls():
    client = Client("positive", fail_first=2)
    job = run(facts({"VNQ": 0.3, "AGG": 0.7}), client)
    assert job.rated == 5 and len(client.calls) == 7


def test_job_ceiling_leaves_the_rest_for_the_background():
    cfg = config()
    cfg.briefing_job.max_pairs = 2
    job = run(facts({"VNQ": 0.3, "AGG": 0.7}), Client("neutral"), cfg=cfg)
    assert job.rated == 2 and job.left_in_background == 3 and job.state == "done"
    states = [a["coverage_status"]["state"] for a in job.result["week"]["instrument_aggregates"]]
    assert set(states) <= {"tone", "quiet", "unrated"}           # final states only, never "rating in progress"


def test_job_without_the_llm_makes_no_calls():
    client = Client()
    job = run(facts({"VNQ": 1.0}), client, env={"EGOQUANT_BRIEFING": "live"})
    assert client.calls == [] and job.state == "done" and job.total == 0


def test_job_fixture_mode_is_offline_and_labeled_demo():
    client = Client()
    job = run(facts({"VNQ": 0.3, "AGG": 0.7}), client, env={"EGOQUANT_BRIEFING": "fixture"})
    assert client.calls == [] and job.result["week"]["banners"][0].startswith("DEMO")
    assert job.result["month"]["briefing_period"] == "month"


def test_job_off_mode_returns_no_briefing():
    job = run(facts({"VNQ": 1.0}), Client(), env={"EGOQUANT_BRIEFING": "off"})
    assert job.state == "done" and job.result["week"] is None


def test_unknown_job_and_failures_are_safe():
    assert jobs.get_job("nope") is None
    job = jobs.start_job(facts(), config=config(), env=LIVE, synchronous=True,
                         runner=lambda job, *a, **k: jobs.run_job(job, *a, **k, get=lambda u: (_ for _ in ()).throw(OSError()),
                                                                   tone_client_factory=lambda t: Client(), finbert=False))
    assert job.state in ("done", "failed")


# ---- worker ---------------------------------------------------------------------------------------------------------
def test_worker_switch_and_reasons():
    assert worker_enabled({**LIVE, "EGOQUANT_NEWS_WORKER": "off"}) == (False, "EGOQUANT_NEWS_WORKER=off")
    assert worker_enabled({"EGOQUANT_BRIEFING": "fixture"})[0] is False
    assert worker_enabled({"EGOQUANT_BRIEFING": "live"})[0] is False                 # no AI tone switch / key
    assert worker_enabled({**LIVE, "EGOQUANT_DATA": "synthetic"})[0] is False
    assert worker_enabled(LIVE) == (True, "on")


def test_worker_plans_core_funds_first_within_the_cap():
    cfg = config()
    t = worker_tickers(cfg)
    assert len(t) <= cfg.news_worker.max_tickers and t[0] in ("BIL", "SHY", "AGG", "VT", "VTI") and "VNQ" in t


def test_worker_run_rates_new_pairs_within_the_caps(tmp_path):
    cfg = config()
    cfg.news_worker.labels_per_run = 3
    client = Client("negative")
    w = NewsWorker(cfg, env=LIVE, get=Get(), client_factory=lambda t: client, now=lambda: AS_OF)
    s = w.run_once()
    assert s["pairs_rated"] == 3 and s["pairs_left"] >= 1 and s["labels_today"] == 3 and s["runs"] == 1
    assert s["new_in_archive"] >= 5 and s["last_error"] is None
    assert json.loads((tmp_path / "briefing" / "news_archive.json").read_text())
    s2 = w.run_once()                                     # the rated pairs are cached: only the rest is asked
    assert s2["pairs_found"] == s["pairs_found"] - 3
    used = s2["labels_today"]
    cfg.news_worker.labels_per_day = used                 # the day cap is reached: nothing more is asked today
    calls = len(client.calls)
    s3 = w.run_once()
    assert s3["pairs_rated"] == 0 and s3["labels_today"] == used and len(client.calls) == calls


# ---- routes ---------------------------------------------------------------------------------------------------------
def test_routes_start_and_poll(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    monkeypatch.setenv("EGOQUANT_BRIEFING", "fixture")
    c = TestClient(app)
    bad = c.post("/briefing/start", json={"briefing_context": "x" * 40})
    assert bad.status_code == 400
    token = jobs.seal_briefing_context(facts({"VNQ": 0.3, "AGG": 0.7}))
    started = c.post("/briefing/start", json={"briefing_context": token}).json()
    for _ in range(100):
        status = c.get(f"/briefing/{started['job']}").json()
        if status["state"] != "running":
            break
        time.sleep(0.05)
    assert status["state"] == "done" and status["result"]["week"]["briefing_period"] == "week"
    assert c.get("/briefing/does-not-exist").status_code == 404


def test_portfolio_still_returns_the_old_fields_and_a_sealed_context(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from egoquant.demo.personas import PERSONAS
    from tests.test_egoquant_routes import portfolio_body
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "fixture")
    persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
    out = TestClient(app).post("/portfolio", json=portfolio_body(persona)).json()
    assert out["report"]["market_briefing"] and "market_briefing_month" in out["report"]
    f = jobs.open_briefing_context(out["briefing_context"])
    assert f["weights"] and "VNQ" in f["interest_tickers"] or f["interest_areas"]


def test_worker_day_count_survives_a_restart():
    cfg = config()
    cfg.news_worker.labels_per_run = 2
    w = NewsWorker(cfg, env=LIVE, get=Get(), client_factory=lambda t: Client("neutral"), now=lambda: AS_OF)
    assert w.run_once()["labels_today"] == 2
    cfg.news_worker.labels_per_day = 2
    client = Client("neutral")
    again = NewsWorker(cfg, env=LIVE, get=Get(), client_factory=lambda t: client, now=lambda: AS_OF)   # a restart
    s = again.run_once()
    assert s["labels_today"] == 2 and s["pairs_rated"] == 0 and client.calls == []
