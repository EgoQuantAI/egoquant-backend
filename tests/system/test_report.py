"""Tests for the client report. Run with:  python -m pytest tests/system/test_report.py -v

The report must (1) quote the numbers of the result exactly, (2) explain behavioral influence honestly,
(3) be reproducible, (4) never put unescaped text into the HTML, (5) say so when it is only a demo.
"""

from datetime import date

import pytest

from egoquant.agents.quant import DEFAULT_STRESS_SCENARIOS, QuantAgent
from egoquant.demo.stubs import StubBehavioralAgent, SyntheticMarketAgent
from egoquant.models import AgentConfig, InvestorContext
from egoquant.report import LimitCheck, _check, build_client_report, render_html

AS_OF = date(2026, 9, 30)
HOLDINGS = {"EQ_GLOBAL": 0.30, "BOND_GOVT": 0.50, "CASH_MM": 0.20}


def _run(risk_class="C4", confidence=0.8, max_equity_b=0.50, holdings=HOLDINGS, config=None, loss=0.25, **ctx):
    market = SyntheticMarketAgent(seed=7)
    config = config or AgentConfig(run_sensitivity=False)
    context = InvestorContext(investor_id="t", risk_class=risk_class, goal_type="growth", horizon_years=10,
                              min_cash_buffer=0.05, max_acceptable_loss=loss, current_holdings=holdings, **ctx)
    result = QuantAgent(market, StubBehavioralAgent(max_equity=max_equity_b, confidence=confidence), config).run(context, AS_OF)
    assets = market.get_market_snapshot(None, AS_OF, config.lookback_days).assets
    return result, context, assets, config


@pytest.fixture(scope="module")
def case():
    result, context, assets, config = _run()
    return result, build_client_report(result, context, assets, config, DEFAULT_STRESS_SCENARIOS), context


def test_numbers_match_the_result(case):
    result, report, _ = case
    final = result.final
    assert report.equity_weight == pytest.approx(final.equity_weight)
    assert report.ex_ante_vol == pytest.approx(final.risk.ex_ante_vol)
    assert sum(a.weight for a in report.allocation) == pytest.approx(1.0, abs=1e-6)
    assert report.equity_weight + report.bond_weight + report.cash_weight == pytest.approx(1.0, abs=1e-6)
    assert sum(a.risk_share for a in report.allocation) == pytest.approx(1.0, abs=1e-6)
    # worst loss is the most negative scenario, and money = return x reference amount
    assert report.worst_loss == pytest.approx(-min(final.stress.values()))
    for row in report.stress:
        assert row.amount_change == pytest.approx(row.portfolio_return * report.reference_amount)


def test_limits_are_checked_against_class_and_investor_numbers(case):
    _, report, context = case
    by_name = {c.name: c for c in report.limits}
    assert by_name["Share in stocks"].limit == pytest.approx(0.70)         # C4 max equity
    assert by_name["Estimated volatility"].limit == pytest.approx(0.13)    # C4 max vol
    assert by_name["Loss in a severe fall"].limit == pytest.approx(0.25)   # min(class 28%, investor 25%)
    assert by_name["Cash buffer"].direction == "min"
    assert report.limits_ok and all(c.ok for c in report.limits)


def test_behavioral_influence_is_explicit_with_source_and_confidence(case):
    _, report, _ = case
    text = " ".join(r.text for r in report.reasons if r.kind == "behavioral")
    assert "50%" in text and "80%" in text and "behavioural" in text
    assert any(c.name == "Stocks, behavioural limit" for c in report.limits)


def test_weak_behavioral_evidence_is_reported_as_not_applied():
    result, context, assets, config = _run(confidence=0.3)
    report = build_client_report(result, context, assets, config)
    assert any(r.kind == "ignored" and "did not apply" in r.text for r in report.reasons)
    assert not any(r.kind == "behavioral" for r in report.reasons)
    assert not any(c.name == "Stocks, behavioural limit" for c in report.limits)
    assert "Stocks, behavioural limit" not in render_html(report)


def test_behavioral_cap_is_quoted_in_the_reasons():
    result, context, assets, config = _run(max_equity_b=0.35)
    report = build_client_report(result, context, assets, config)
    assert report.equity_weight <= 0.35 + 1e-6
    assert any("35%" in r.text for r in report.reasons if r.kind == "behavioral")


def test_equity_reduction_is_explained_when_a_loss_limit_forces_it():
    result, context, assets, config = _run(max_equity_b=0.90, loss=0.10)       # B's cap is loose, the loss limit is not
    report = build_client_report(result, context, assets, config)
    assert 0 < report.equity_weight < 0.55
    assert any("lowered the share in stocks" in r.text for r in report.reasons)
    assert any(r.kind == "limit" and "10%" in r.text for r in report.reasons)


def test_report_and_html_are_reproducible(case):
    result, report, context = case
    result2, context2, assets2, config2 = _run()
    report2 = build_client_report(result2, context2, assets2, config2, DEFAULT_STRESS_SCENARIOS)
    assert report.model_dump_json() == report2.model_dump_json()
    assert render_html(report) == render_html(report2)
    assert report.input_hash and report.input_hash in render_html(report)


def test_html_escapes_untrusted_text():
    result, context, assets, config = _run()
    nasty = '<script>alert("x")</script>'
    report = build_client_report(result, context, assets, config, asset_names={"EQ_GLOBAL": nasty})
    page = render_html(report)
    assert nasty not in page and "&lt;script&gt;" in page


def test_trades_only_with_current_holdings():
    result, context, assets, config = _run(holdings=None)
    report = build_client_report(result, context, assets, config)
    assert report.trades == [] and report.turnover is None
    assert "first purchase" in render_html(report)
    result, context, assets, config = _run()
    with_holdings = build_client_report(result, context, assets, config)
    assert {t.asset_id for t in with_holdings.trades} >= set(HOLDINGS)
    assert sum(t.change for t in with_holdings.trades) == pytest.approx(0.0, abs=1e-6)


def test_banners_say_demo_and_placeholder_and_disappear_when_not_true():
    result, context, assets, config = _run()
    report = build_client_report(result, context, assets, config)
    assert any("synthetic" in b for b in report.banners) and any("placeholder" in b for b in report.banners)
    policies = dict(config.risk_class_policies)
    policies["C5"] = policies["C5"].model_copy(update={"target_equity": 0.75})
    custom = AgentConfig(run_sensitivity=False, risk_class_policies=policies)
    result, context, assets, config = _run(config=custom)
    assert not any("placeholder" in b for b in build_client_report(result, context, assets, config).banners)


def test_infeasible_result_still_renders_a_clear_page():
    # C1 with a 10% holding cap over six assets can never reach 100% invested
    result, context, assets, config = _run(risk_class="C1", max_asset_weight=0.10, holdings=None)
    assert result.final.status == "infeasible"
    report = build_client_report(result, context, assets, config)
    page = render_html(report)
    assert report.status == "infeasible" and "No portfolio proposed" in page and "<svg" not in page


def test_limit_check_direction_logic():
    assert _check("x", 0.5, 0.6, "max", "").ok and not _check("x", 0.7, 0.6, "max", "").ok
    assert _check("x", 0.1, 0.05, "min", "").ok and not _check("x", 0.03, 0.05, "min", "").ok
    assert isinstance(_check("x", 0.5, 0.6, "max", ""), LimitCheck)


def test_page_has_every_section_and_a_table_view_for_each_chart(case):
    _, report, _ = case
    page = render_html(report)
    for heading in ["Summary", "What you would hold", "Why this portfolio", "How risky is it?", "Where the risk comes from",
                    "What could happen", "Options we considered", "Changes and costs", "Assumptions and important information"]:
        assert heading in page
    assert page.count("<table>") + page.count("<div class='tw'><table>") >= 5
    assert "aria-label" in page and "<title>" in page


def test_no_duplicate_reasons_and_loss_limit_scope_is_stated():
    # C1 with a 40% holding cap: several assets sit on the cap, but the sentence must appear once
    result, context, assets, config = _run(risk_class="C1", liquidity_need=0.05, suitability_max_equity=0.8,
                                           config=AgentConfig(run_sensitivity=False, turnover_penalty=25.0))
    assert len([l for l in result.final.binding_constraints if "max_asset_weight" in l]) >= 2   # the case being tested
    report = build_client_report(result, context, assets, config)
    texts = [r.text for r in report.reasons]
    assert any("Government bonds" in t and "Money-market" in t for t in texts if "single holding" in t)
    assert len(texts) == len(set(texts))
    page = render_html(report)
    assert "Your loss limit applies to one test" in page and report.limit_scenario in page


def test_behavioral_loss_limit_is_checked_and_named_when_it_lowers_stocks():
    _, context, assets, config = _run()
    stub = StubBehavioralAgent(max_equity=1.0, max_stress_loss=0.10, confidence=0.9)
    result = QuantAgent(SyntheticMarketAgent(seed=7), stub, config).run(context, AS_OF)
    report = build_client_report(result, context, assets, config)
    row = next(c for c in report.limits if c.name == "Loss in a severe fall, behavioural limit")
    assert row.limit == pytest.approx(0.10) and row.ok
    lowered = [r for r in report.reasons if r.text.startswith("We lowered the share in stocks")]
    assert len(lowered) == 1 and lowered[0].kind == "behavioral"
    assert "behavioural limit of a loss of at most 10%" in lowered[0].text


def test_risk_engine_verdict_is_stored_and_shown_only_when_rejected():
    from egoquant.risk_engine import RiskReview
    result, context, assets, config = _run()
    approved = build_client_report(result, context, assets, config,
                                   risk_review=RiskReview(approved=True, violations=[], checks=[]))
    assert approved.risk_review_approved is True
    assert not any("NOT APPROVED" in b for b in approved.banners)
    rejected = build_client_report(result, context, assets, config,
                                   risk_review=RiskReview(approved=False, violations=["VT is R3; investor maximum is R1"], checks=[]))
    assert rejected.risk_review_approved is False
    assert any("NOT APPROVED" in b and "VT is R3" in b for b in rejected.banners)
    assert "NOT APPROVED" in render_html(rejected)
    assert build_client_report(result, context, assets, config).risk_review_approved is None


def test_commodity_holdings_are_split_out_labelled_and_capped():
    from egoquant.agents.behavioral import NoBehavioralEvidence
    from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource

    policies = dict(AgentConfig().risk_class_policies)
    policies["C3"] = policies["C3"].model_copy(update={"target_equity": 0.0})   # all-bond end holds gold here
    config = AgentConfig(run_sensitivity=False, risk_class_policies=policies)
    agent_c = MarketIntelligenceAgent(InvestorInterests(investor_id="g", interest_areas=["gold", "stocks"]),
                                      source=SyntheticPriceSource())
    context = InvestorContext(investor_id="g", risk_class="C3", goal_type="growth", horizon_years=10,
                              min_cash_buffer=0.05)
    agent = QuantAgent(agent_c, NoBehavioralEvidence(), config)
    result = agent.run(context, AS_OF)
    assets = agent_c.get_market_snapshot(None, AS_OF, config.lookback_days).assets
    report = build_client_report(result, context, assets, config, agent.scenarios, selection=agent_c.selection)
    assert report.commodity_weight > 0.005
    total = report.equity_weight + report.bond_weight + report.cash_weight + report.commodity_weight + report.crypto_weight
    assert total == pytest.approx(1.0)
    assert "gold & commodities" in report.headline
    gold_cap = next(c for c in report.limits if c.name == "Share in gold & commodities")
    assert gold_cap.limit == pytest.approx(0.10) and gold_cap.ok
    page = render_html(report)
    assert "c-commodity" in page and f"worst of {len(DEFAULT_STRESS_SCENARIOS)} hypothetical scenarios" in page
    assert "What you asked about" in page


def test_long_fund_names_are_shortened_in_charts_but_kept_in_tooltips_and_tables():
    from egoquant.report import _fit, _svg_hbar

    name = "Invesco Optimum Yield Diversified Commodity Strategy ETF"
    short = _fit(name, 190)
    assert short.endswith("…") and len(short) < len(name)
    svg = _svg_hbar([(name, 0.1, "c-commodity", "10%", f"{name}: 10%")])
    assert f">{short}</text>" in svg and f"<title>{name}: 10%</title>" in svg
    assert _fit("Gold", 190) == "Gold"
