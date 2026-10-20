"""One source of truth: the report's Agent D section (HTML download, report view, "What changed", Advisor chat
context, saved account report) is the briefing JOB's final version, the same as the News tab's. Offline: synthetic
prices, sample news, no network, no OpenAI."""

from __future__ import annotations

import copy
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from egoquant.briefing import final, jobs, web
from tests.accounts.test_accounts import A, FakeStore
from egoquant.accounts.store import set_store
from egoquant.accounts import routes as account_routes


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "fixture")
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path))
    jobs.clear_jobs()
    web.clear_feed_cache()
    from main import app
    yield TestClient(app)
    jobs.clear_jobs()


def portfolio(client, path="/portfolio", headers=None):
    from egoquant.demo.personas import PERSONAS
    from tests.test_egoquant_routes import portfolio_body
    persona = next(p for p in PERSONAS.values() if p.expected_class == "C3")
    r = client.post(path, json=portfolio_body(persona), headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def finished_job(client, out) -> dict:
    job = client.post("/briefing/start", json={"briefing_context": out["briefing_context"]}).json()["job"]
    for _ in range(200):
        status = client.get(f"/briefing/{job}").json()
        if status["state"] != "running":
            break
        time.sleep(0.05)
    assert status["state"] == "done"
    return {"job": job, **status}


def without_agent_d(report: dict) -> dict:
    return {k: v for k, v in report.items() if k not in final.AGENT_D_KEYS}


# ---- the digest ------------------------------------------------------------------------------------------------
def test_digest_ignores_agent_d_sections_and_number_spelling():
    r = {"allocation": [{"asset_id": "BIL", "weight": 1.0}], "market_briefing": {"x": 1}, "banners": ["DEMO"]}
    same = {"allocation": [{"asset_id": "BIL", "weight": 1}], "what_changed": ["other"]}     # browser: 1.0 -> 1
    assert final.report_digest(r) == final.report_digest(same)
    changed = {"allocation": [{"asset_id": "BIL", "weight": 0.99}]}
    assert final.report_digest(r) != final.report_digest(changed)


# ---- the report takes the job's final briefing --------------------------------------------------------------------
def test_report_agent_d_section_equals_the_jobs_final_briefing(client):
    out = portfolio(client)
    done = finished_job(client, out)
    r = client.post(f"/briefing/{done['job']}/report",
                    json={"report": without_agent_d(out["report"]), "chat_context": out["chat_context"]})
    assert r.status_code == 200, r.text
    got = r.json()
    week, month = done["result"]["week"], done["result"]["month"]
    report = got["report"]
    assert report["market_briefing"] == week and report["market_briefing_month"] == month
    assert report["what_changed"] == done["result"]["what_changed"]
    assert report["what_changed_action"] == done["result"]["what_changed_action"]
    # same counts, ratings and period as the News tab
    assert report["market_briefing"]["metadata"]["n_fund_tones_llm"] == week["metadata"]["n_fund_tones_llm"]
    assert [a["tone"] for a in report["market_briefing"]["instrument_aggregates"]] == \
        [a["tone"] for a in week["instrument_aggregates"]]
    # Agent A's and the Risk Engine's numbers are untouched
    assert without_agent_d(report) == without_agent_d(out["report"])
    # the HTML download is rendered from the final version, with the news period beside the price date
    html = got["html"]
    start = datetime.fromisoformat(week["period_start"]).strftime("%d %b")
    assert f"News {start} to " in html and "prices as of" in html
    assert "not yet assessed" not in html and "cache only" not in html
    # the Advisor's sealed context now carries the final briefing
    from egoquant.advisor.context import _briefing_facts, open_context
    facts = open_context(got["chat_context"])
    assert facts["market_briefing"] == _briefing_facts(week)
    assert facts["c_level"] == open_context(out["chat_context"])["c_level"]


def test_a_changed_number_is_refused(client):
    out = portfolio(client)
    done = finished_job(client, out)
    tampered = copy.deepcopy(without_agent_d(out["report"]))
    tampered["allocation"][0]["weight"] = round(tampered["allocation"][0]["weight"] + 0.01, 6)
    r = client.post(f"/briefing/{done['job']}/report", json={"report": tampered})
    assert r.status_code == 409 and "does not belong" in r.json()["detail"]


def test_download_before_the_rating_is_done_waits(client):
    out = portfolio(client)
    facts = jobs.open_briefing_context(out["briefing_context"])
    job = jobs.start_job(facts, runner=lambda *a, **k: None, synchronous=True)          # stays "running"
    job.rated, job.total = 12, 40
    r = client.post(f"/briefing/{job.id}/report", json={"report": without_agent_d(out["report"])})
    assert r.status_code == 409 and r.json()["detail"] == "The news is still being rated (12 of 40)."
    assert client.post("/briefing/nope/report", json={"report": {}}).status_code == 404


def test_a_finished_job_names_the_plain_reason_for_any_unrated_headline():
    from egoquant.briefing.render import models_line
    from tests.briefing.common import build, item
    b = build([item("REITs fall as office vacancies climb", 1)], weights={"VNQ": 1.0})
    b.metadata.n_fund_tones_unavailable = 3
    b.metadata.tone_fallback_reasons = ["the rating limit for one briefing was reached; the rest is rated in the "
                                        "background"]
    line = models_line(b)
    assert "not rated (3: the rating limit for one briefing was reached" in line and "not yet assessed" not in line


# ---- the saved account report gets the final version too ------------------------------------------------------
class SavingStore(FakeStore):
    def rpc(self, token, name, args):
        if name != "replace_report_briefing":
            return super().rpc(token, name, args)
        assert set(args["sections"]) <= set(final.AGENT_D_KEYS)          # the function accepts only these keys
        rows = [r for r in self._rls(token, "reports") if r["id"] == args["rid"]]
        if not rows:
            raise AssertionError("report not found")
        rows[0]["report"]["report"].update(args["sections"])
        return None


@pytest.fixture
def saving_store():
    store = SavingStore()
    set_store(store)
    account_routes._user_hits.clear()
    yield store
    set_store(None)


def test_saved_report_takes_the_final_briefing(client, saving_store):
    out = portfolio(client, "/account/portfolio", headers=A)
    done = finished_job(client, out)
    r = client.post(f"/account/reports/{out['report_id']}/briefing", json={"job": done["job"]}, headers=A)
    assert r.status_code == 200, r.text
    saved = saving_store.tables["reports"][0]["report"]["report"]
    assert saved["market_briefing"] == done["result"]["week"] and saved["what_changed"] == done["result"]["what_changed"]
    assert saved["allocation"] == out["report"]["allocation"]
    other = portfolio(client, "/account/portfolio", headers=A)            # another saved report with other numbers
    row = next(x for x in saving_store.tables["reports"] if x["id"] == other["report_id"])
    row["report"]["report"]["allocation"][0]["weight"] += 0.01
    r = client.post(f"/account/reports/{other['report_id']}/briefing", json={"job": done["job"]}, headers=A)
    assert r.status_code == 409 and "does not belong" in r.json()["detail"]
    assert client.post(f"/account/reports/{out['report_id']}/briefing", json={"job": done["job"]},
                       headers={"Authorization": "Bearer token-b"}).status_code == 404       # not B's report


# ---- the in-request briefing: news period ends now, not at the price date ------------------------------------
def test_in_request_news_period_ends_now_not_at_the_price_date():
    from tests.briefing.test_web import LIVE, FakeGet, config, interests, proposal
    now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    b = web.build_web_briefing(interests=interests("real_estate"), selection=None,
                               proposal=proposal({"SPY": 0.7, "VNQ": 0.3}), as_of=date(2026, 10, 16), horizon_years=5,
                               config=config(), env=LIVE, get=FakeGet(), now=lambda: now)
    assert b is not None and b.period_end == now


# ---- the job rates every pair BOTH views show ------------------------------------------------------------------
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
         "romeo sierra tango uniform victor whiskey xray yankee zulu amber bronze cobalt denim ember fern granite "
         "harbor ivory jasper kestrel lagoon meadow nectar orchid pepper quartz river saffron timber umber violet "
         "willow yarrow zephyr acorn birch cedar dune").split()


def test_job_queues_last_weeks_exposure_news_that_the_monthly_view_does_not_reach():
    from egoquant.briefing.fund_tone import FundToneLabeler
    from tests.briefing.common import AS_OF, config, interests, item
    this_week = [item(f"Fed {WORDS[i]} {WORDS[-1 - i]} remarks on the rate path", 0.1 + i * 0.12, source_name="Reuters")
                 for i in range(45)]                                    # more than the monthly cap of 40
    last_week = [item(t, 9 + j * 0.5, source_name="Reuters") for j, t in enumerate((
        "Fed governor sees inflation cooling slowly", "FOMC minutes show split over balance sheet runoff",
        "Federal Reserve keeps discount window terms unchanged"))]
    cfg = config()
    labeler = FundToneLabeler(cfg, precomputed={}, allow_calls=False)
    args = (this_week + last_week, AS_OF, cfg, interests("cash"), None, {"BIL": 1.0}, labeler)
    month_only = {it.id for it, t in web.pending_tone_pairs(*args, limit=10_000, periods=("month",)) if t == "BIL"}
    both = {it.id for it, t in web.pending_tone_pairs(*args, limit=10_000, periods=("week", "month")) if t == "BIL"}
    old = {it.id for it in last_week}
    assert old <= both and not old <= month_only                       # the weekly view's "last week" is rated too
    assert month_only <= both
