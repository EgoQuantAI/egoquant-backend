"""Agent D in the pipeline: optional, after the Risk Engine verdict, never changes a number. Offline."""

from datetime import date

import pytest

from egoquant.agents.market_intel import SyntheticPriceSource
from egoquant.agents.market_intel.agent import DrawdownAlert
from egoquant.briefing.__main__ import make_builder
from egoquant.briefing.aggregate import tone_words
from egoquant.briefing.loader import load_config
from egoquant.briefing.models import DEMO_BANNER, BriefingAlert
from egoquant.cli import main as cli_main
from egoquant.demo.personas import PERSONAS, run_persona
from egoquant.report import RECHECK_OFFER, attach_what_changed, render_html, what_changed_html, what_changed_lines
from egoquant.risk_engine import RiskEngine

AS_OF = date(2026, 10, 16)
PERSONA = PERSONAS["growth_investor"]              # holds VNQ and short Treasuries
REPORT_ONLY = {"market_briefing", "what_changed", "what_changed_action", "banners"}


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path / "cache"))


def run(**kw):
    return run_persona(PERSONA, AS_OF, price_source=SyntheticPriceSource(), **kw)


@pytest.fixture(scope="module")
def plain():
    return run_persona(PERSONA, AS_OF, price_source=SyntheticPriceSource())


@pytest.mark.parametrize("period", ["week", "month"])
def test_weights_verdict_and_report_numbers_identical_with_and_without_the_briefing(plain, period):
    on = run(briefing_builder=make_builder(live=False, allow_llm=False), briefing_period=period)
    b = on.report.market_briefing
    assert b is not None and b.briefing_period == period and DEMO_BANNER in on.report.banners
    assert on.result.final.weights == plain.result.final.weights
    assert on.review.model_dump() == plain.review.model_dump()
    assert on.report.model_dump(exclude=REPORT_ONLY) == plain.report.model_dump(exclude=REPORT_ONLY)
    assert plain.report.market_briefing is None and plain.report.what_changed == []
    assert b.metadata.n_tone_llm_calls == 0 and b.metadata.n_llm_calls == 0


def test_briefing_is_off_by_default_and_period_reaches_the_builder():
    seen = {}

    def builder(*, interests, selection, proposal, as_of, horizon_years, briefing_period):
        seen.update(period=briefing_period, proposal=proposal)
        return None
    run(briefing_builder=builder, briefing_period="month")
    assert seen["period"] == "month" and seen["proposal"] is not None


def test_a_rejected_proposal_sends_no_weights_to_the_briefing():
    real = RiskEngine()

    class Rejecting:
        def review(self, *a, **k):
            return real.review(*a, **k).model_copy(update={"approved": False, "violations": ["test rejection"]})
    seen = {}

    def builder(**kw):
        seen.update(kw)
        return make_builder(live=False, allow_llm=False)(**kw)
    from egoquant.pipeline import run_pipeline
    out = run_pipeline(PERSONA.answers(), investor_id=PERSONA.investor_id, interest_areas=["real_estate", "bonds"],
                       goal_type=PERSONA.goal_type, horizon_years=PERSONA.horizon_years, as_of=AS_OF,
                       price_source=SyntheticPriceSource(), risk_engine=Rejecting(), briefing_builder=builder)
    assert not out.approved and seen["proposal"] is None
    b = out.report.market_briefing
    assert b is not None and b.alerts == [] and all(i.exposure is None for i in b.items)


def test_a_failing_builder_keeps_every_number(plain):
    def broken(**_):
        raise RuntimeError("feeds down")
    out = run(briefing_builder=broken)
    assert out.report.market_briefing is None and out.result.final.weights == plain.result.final.weights
    assert out.report.model_dump(exclude=REPORT_ONLY) == plain.report.model_dump(exclude=REPORT_ONLY)


# ---- "What changed" -----------------------------------------------------------------------------------------------
DD = DrawdownAlert(ticker="VNQ", window_days=21, drawdown_from_peak=-0.123, message="ignored: rebuilt from numbers")


def test_what_changed_is_built_from_the_numbers(plain):
    b = run(briefing_builder=make_builder(live=False, allow_llm=False)).report.market_briefing
    lines = what_changed_lines([DD], b, {"VNQ": "Vanguard Real Estate ETF"}, date(2026, 10, 16), holdings=["VNQ"])
    assert lines[0] == ("Vanguard Real Estate ETF (VNQ) closed 12.3% below its highest close of the last 21 trading "
                        "days (data to 16 Oct 2026). This is past (in-sample) price data, not a forecast.")
    assert lines[1] == b.alerts[0].summary and lines[1].startswith("News tone about VNQ moved from ")
    report = attach_what_changed(plain.report, [DD], b, {"VNQ": "Vanguard Real Estate ETF"}, holdings=["VNQ"])
    assert report.what_changed_action == "offer_risk_profile_recheck"
    html = what_changed_html(report)
    assert "<h2>What changed</h2>" in html and "data-action='offer_risk_profile_recheck'" in html
    assert RECHECK_OFFER in html and html.count("data-action=") == 1
    assert "What changed" not in render_html(report)                     # the download leaves it out (News tab only)
    assert "invented (synthetic) demo prices" in report.what_changed[0]     # plain ran on synthetic prices


def dd(ticker, drop=-0.12):
    return DrawdownAlert(ticker=ticker, window_days=21, drawdown_from_peak=drop, message="")


def test_what_changed_drawdowns_name_holdings_only():
    alerts = [dd("AAPL"), dd("USO"), dd("VNQ"), dd("SHY", -0.11), dd("PFE")]
    lines = what_changed_lines(alerts, None, holdings=["SHY", "VNQ"], interest_tickers={"AAPL"},
                               drawdown_threshold=0.10, synthetic=True)
    assert [x.split(" ")[0] for x in lines] == ["SHY", "VNQ"]          # no interest-area or universe lines
    assert all("invented (synthetic) demo prices" in x for x in lines)
    assert what_changed_lines(alerts, None) == []                      # no holdings: no drawdown lines


def with_holding(report, ticker, weight):
    row = report.allocation[0].model_copy(update={"asset_id": ticker, "weight": weight})
    return report.model_copy(update={"allocation": [row]})


def test_held_drawdown_above_5_percent_offers_the_recheck(plain):
    big = with_holding(plain.report, "VNQ", 0.30)
    r = attach_what_changed(big, [dd("VNQ", -0.12)], None, holdings=["VNQ"], drawdown_threshold=0.10)
    assert r.what_changed[0].startswith("VNQ") and r.what_changed_action == "offer_risk_profile_recheck"
    assert "data-action='offer_risk_profile_recheck'" in what_changed_html(r)
    small = attach_what_changed(with_holding(plain.report, "VNQ", 0.05), [dd("VNQ", -0.12)], None, holdings=["VNQ"],
                                drawdown_threshold=0.10)
    assert small.what_changed and small.what_changed_action is None                 # 5% is not "more than 5%"
    shallow = attach_what_changed(big, [dd("VNQ", -0.08)], None, holdings=["VNQ"], drawdown_threshold=0.10)
    assert shallow.what_changed_action is None                                      # under 10% from the high
    interest = attach_what_changed(big, [dd("AAPL", -0.30)], None, holdings=["VNQ"], interest_tickers={"AAPL"},
                                   drawdown_threshold=0.10)
    assert interest.what_changed == [] and interest.what_changed_action is None     # interest drops: never


def test_job_offers_the_recheck_for_a_held_drawdown():
    from egoquant.agents.market_intel import InvestorInterests
    from egoquant.briefing import jobs
    def facts(weights, dd_ticker):
        return jobs.context_facts(
            interests=InvestorInterests(investor_id="x", interest_areas=["stocks"], max_r_level=4), watchlist=[],
            weights=weights, horizon_years=10, drawdowns=[dd(dd_ticker, -0.15)], names={}, data_to=None,
            synthetic=True, drawdown_threshold=0.10, interest_tickers=["AAPL"])
    assert jobs._offer(facts({"VNQ": 0.4, "BIL": 0.6}, "VNQ"), None) is True
    assert jobs._offer(facts({"VNQ": 0.04, "BIL": 0.96}, "VNQ"), None) is False
    assert jobs._offer(facts({"VNQ": 0.4, "BIL": 0.6}, "AAPL"), None) is False


def test_recheck_offer_only_for_negative_tone_moves(plain):
    from egoquant.briefing.models import BriefingAlert
    b = run(briefing_builder=make_builder(live=False, allow_llm=False)).report.market_briefing
    assert b.alerts and all(a.change < 0 and a.action == "offer_risk_profile_recheck" for a in b.alerts)
    up = b.model_copy(update={"alerts": [BriefingAlert(subject="VNQ", kind="instrument", change=0.8, exposure=0.3,
                                                       message="News tone about VNQ moved up.", summary="News tone about VNQ moved up.",
                                                       action=None)]})
    report = attach_what_changed(plain.report, [], up, holdings=["VNQ"])
    assert report.what_changed == ["News tone about VNQ moved up."] and report.what_changed_action is None
    assert "data-action=" not in what_changed_html(report)
    report = attach_what_changed(with_holding(plain.report, "VNQ", 0.03), [DD], None, holdings=["VNQ"])
    assert report.what_changed and report.what_changed_action is None   # a 3% holding's drawdown: listed, no offer
    with pytest.raises(ValueError):                                     # a rise never carries the offer
        b.model_copy(update={"alerts": []}).model_validate(
            {**b.model_dump(), "alerts": [{"subject": "VNQ", "kind": "instrument", "change": 0.8, "exposure": 0.3,
                                           "message": "x", "action": "offer_risk_profile_recheck"}]})


def test_fixture_area_alert_with_the_same_headlines_is_not_repeated():
    b = run(briefing_builder=make_builder(live=False, allow_llm=False)).report.market_briefing
    subjects = [a.subject for a in b.alerts]
    assert subjects == ["VNQ"]                                       # real estate: same headlines as VNQ
    area = next(a for a in b.area_aggregates if a.subject == "real_estate")
    inst = next(a for a in b.instrument_aggregates if a.subject == "VNQ")
    assert area.item_ids == inst.item_ids and area.change is not None and area.exposure > inst.exposure


@pytest.mark.parametrize("period, this, last", [("week", "this week", "last week"), ("month", "this month", "last month")])
def test_alert_says_levels_and_change(period, this, last):
    def tone_label(v):
        return tone_words(v, load_config())
    b = run(briefing_builder=make_builder(live=False, allow_llm=False), briefing_period=period).report.market_briefing
    a = b.alerts[0]
    assert a.message == (f"News tone about VNQ moved from {tone_label(a.tone_previous)} ({a.tone_previous:+.2f}) {last} "
                         f"to {tone_label(a.tone)} ({a.tone:+.2f}) {this} "
                         f"(change {a.change:+.2f}, {a.n_items} headlines). It concerns {a.exposure:.0%} of your "
                         "portfolio. Tone is an assessment of the headlines, not a forecast of prices. Would you like "
                         "to re-check your risk profile? Nothing changes unless you confirm.")
    assert a.change == pytest.approx(a.tone - a.tone_previous)


def test_header_names_what_actually_scored_the_items():
    from egoquant.briefing.render import briefing_section_html, models_line
    b = run(briefing_builder=make_builder(live=False, allow_llm=False)).report.market_briefing
    m = b.metadata
    assert models_line(b) == (f"Tone: fund-aware gpt-4.1-mini ({m.n_fund_tones_llm}), FinBERT fallback (0); "
                              f"summaries: {m.llm_model} ({m.prompt_version}) or a fixed template.")
    html = briefing_section_html(b)
    assert "Tone model" not in html and "ProsusAI" not in html


def test_what_changed_drops_advice_lines_and_has_no_action_when_empty(plain):
    lines = what_changed_lines([DD], None, {"VNQ": "You should buy now"}, holdings=["VNQ"])
    assert lines == []
    report = attach_what_changed(plain.report, [], None)
    assert report.what_changed == [] and report.what_changed_action is None
    assert "What changed" not in what_changed_html(report)


# ---- CLI -----------------------------------------------------------------------------------------------------------
def test_cli_advise_with_briefing_and_period(tmp_path, capsys):
    cli_main(["advise", "growth_investor", "--synthetic", "--as-of", "2026-10-16", "--briefing", "--period", "month",
              "--out", str(tmp_path)])
    html = (tmp_path / "growth_investor_2026-10-16.html").read_text()
    # the download leaves the news and its DEMO banner out (News tab only)
    assert "Market Briefing" not in html and "Agent D" not in html and "DEMO: sample news items" not in html
    assert "AGENT D briefing (month, fixture)" in capsys.readouterr().out


def test_cli_period_needs_briefing(tmp_path):
    with pytest.raises(SystemExit):
        cli_main(["advise", "growth_investor", "--synthetic", "--period", "month", "--out", str(tmp_path)])
