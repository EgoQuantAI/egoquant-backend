"""Tests for the Risk Engine and the R-level path (questionnaire -> Agent C -> Agent A -> Risk Engine).

Run with:  pytest -v tests/system/test_risk_engine.py      (SYNTHETIC prices; no internet needed)
"""

from datetime import date

import pytest

from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource
from egoquant.agents.quant import QuantAgent
from egoquant.models import AgentConfig, AssetInfo, Constraint, InvestorContext, PortfolioProposal
from egoquant.risk_engine import RiskEngine
from egoquant.risk_profile import (
    build_risk_profile,
    to_agent_c_limits,
    to_constraints,
    to_investor_context_limits,
)
from tests.helpers import ASSESSED_AT, BANK, CONFIG, make_answers

AS_OF = date(2026, 9, 30)
SCENARIO = CONFIG.adapter.stress_scenario
R_LEVELS = RiskEngine().r_levels


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def c1_by_capacity_profile():
    """Tolerance level 4, but 48,000 of 50,000 is needed back: loss capacity 4% -> C1 via capacity."""
    profile = build_risk_profile(make_answers(gl_score=30, invested=50_000, needed=48_000), BANK, CONFIG, assessed_at=ASSESSED_AT)
    assert (profile.c_level, profile.binding_dimension, profile.no_loss_gate) == ("C1", "capacity", False)
    assert profile.loss_capacity == pytest.approx(0.04)
    return profile


def run_agents(interests: InvestorInterests, context_fields: dict, risk_class: str):
    agent_c = MarketIntelligenceAgent(interests, source=SyntheticPriceSource())
    context = InvestorContext(investor_id="t", risk_class=risk_class, goal_type="house", horizon_years=3,
                              **context_fields)
    result = QuantAgent(agent_c, NoBehavioralEvidence(), AgentConfig(run_sensitivity=False)).run(context, AS_OF)
    assets = agent_c.get_market_snapshot(agent_c.selection.tickers, AS_OF, AgentConfig().lookback_days).assets
    return agent_c, result, assets


def run_pipeline(profile, areas=("stocks",)):
    """The intended wiring: profile -> Agent C limits -> Agent A context limits -> Agent A."""
    interests = InvestorInterests(investor_id="t", interest_areas=list(areas), **to_agent_c_limits(profile, CONFIG))
    agent_c = MarketIntelligenceAgent(interests, source=SyntheticPriceSource())
    fields = to_investor_context_limits(profile, CONFIG, agent_c.selection)
    return run_agents(interests, fields, profile.c_level)


def held(result) -> dict[str, float]:
    return {a: w for a, w in result.final.weights.items() if w > 1e-6}


def proposal(weights: dict[str, float]) -> PortfolioProposal:
    return PortfolioProposal(stage="constrained", method="test", as_of=AS_OF, status="optimal", weights=weights)


def assets() -> list[AssetInfo]:
    return [
        AssetInfo(asset_id="BIL", asset_class="cash"),
        AssetInfo(asset_id="SHY", asset_class="bond", duration_years=1.9, convexity=5.0),
        AssetInfo(asset_id="AGG", asset_class="bond", duration_years=6.0, convexity=50.0),
        AssetInfo(asset_id="VT", asset_class="equity"),
    ]


def limits(max_equity: float = 0.0, max_loss: float = 0.04) -> list[Constraint]:
    return [Constraint(type="max_equity", value=max_equity, source="suitability"),
            Constraint(type="max_stress_loss", value=max_loss, source="suitability", scenario=SCENARIO)]


# ==========================================================================
# 1. End to end: questionnaire -> Agent C -> Agent A -> Risk Engine
# ==========================================================================
@pytest.mark.parametrize("areas", [("stocks",), ("bonds", "government_bonds"), ("technology", "crypto")])
def test_c1_by_capacity_with_4pct_loss_capacity_gets_no_asset_above_r1(areas):
    profile = c1_by_capacity_profile()
    agent_c, result, asset_infos = run_pipeline(profile, areas)

    assert result.final.status != "infeasible"
    holdings = held(result)
    assert holdings, "C1 must still get a portfolio"
    assert all(R_LEVELS[a] <= 1 for a in holdings), holdings
    for portfolio in result.menu.portfolios:              # the menu Agent B sees is clean too
        assert all(R_LEVELS[a] <= 1 for a, w in portfolio.weights.items() if w > 1e-6)

    review = RiskEngine().review(result.final, asset_infos, to_constraints(profile, CONFIG), profile.max_r_level)
    assert review.approved, review.violations


def test_without_r_level_an_r2_bond_slips_in_and_the_risk_engine_rejects_it():
    """The gap from the build report: same investor, but nobody passes max_r_level along."""
    profile = c1_by_capacity_profile()
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"])        # no max_r_level
    _, result, asset_infos = run_agents(
        interests, {"suitability_max_equity": 0.0, "max_acceptable_loss": profile.loss_capacity}, "C1")

    assert any(R_LEVELS[a] > 1 for a in held(result))                               # e.g. AGG (R2)
    review = RiskEngine().review(result.final, asset_infos, to_constraints(profile, CONFIG), profile.max_r_level)
    assert not review.approved
    assert any("is R2; investor maximum is R1" in v for v in review.violations)


def test_gate_investor_gets_an_approved_no_loss_portfolio():
    """No-loss investors (gate): Agent A receives the 0% loss limit and holds only assets that do not
    fall in the stress scenario; the Risk Engine approves."""
    gate = build_risk_profile(make_answers(gl_score=30, D1="no"), BANK, CONFIG, assessed_at=ASSESSED_AT)
    _, result, asset_infos = run_pipeline(gate)
    assert result.final.status != "infeasible"
    assert result.final.stress[SCENARIO] >= -1e-9
    review = RiskEngine().review(result.final, asset_infos, to_constraints(gate, CONFIG), gate.max_r_level)
    assert review.approved, review.violations


def test_adapter_backstop_excludes_assets_above_max_r_level():
    """If Agent C did not filter (no max_r_level given), the adapter still excludes higher-rated assets."""
    profile = c1_by_capacity_profile()
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"])
    agent_c = MarketIntelligenceAgent(interests, source=SyntheticPriceSource())
    fields = to_investor_context_limits(profile, CONFIG, agent_c.selection)
    assert set(fields["excluded_assets"]) == {t for t in agent_c.selection.tickers if R_LEVELS[t] > 1}
    _, result, _ = run_agents(interests, fields, "C1")
    assert all(R_LEVELS[a] <= 1 for a in held(result))


def test_c1_has_two_r1_assets_and_needs_the_r1_cap():
    """Agent C's catalog has two R1 funds; Agent A's default 40% cap cannot reach 100% with them."""
    profile = c1_by_capacity_profile()
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"], **to_agent_c_limits(profile, CONFIG))
    agent_c = MarketIntelligenceAgent(interests, source=SyntheticPriceSource())
    assert sorted(agent_c.selection.tickers) == ["BIL", "SHY"]
    fields = to_investor_context_limits(profile, CONFIG, agent_c.selection)
    assert fields["max_asset_weight"] == CONFIG.adapter.r1_max_asset_weight

    _, default_cap, _ = run_agents(interests, {**fields, "max_asset_weight": 0.40}, "C1")
    assert default_cap.final.status == "infeasible"


def test_r1_cap_is_not_applied_above_c1():
    profile = build_risk_profile(make_answers(gl_score=25), BANK, CONFIG, assessed_at=ASSESSED_AT)               # C3
    interests = InvestorInterests(investor_id="t", interest_areas=["stocks"], **to_agent_c_limits(profile, CONFIG))
    fields = to_investor_context_limits(profile, CONFIG, MarketIntelligenceAgent(
        interests, source=SyntheticPriceSource()).selection)
    assert "max_asset_weight" not in fields
    assert fields["excluded_assets"] == []


# ==========================================================================
# 2. Risk Engine unit tests (hand-made proposals)
# ==========================================================================
def test_clean_c1_portfolio_is_approved():
    review = RiskEngine().review(proposal({"BIL": 0.4, "SHY": 0.6}), assets(), limits(), max_r_level=1)
    assert review.approved, review.violations
    assert {c.name for c in review.checks} >= {"weights", "r_level[BIL]", "r_level[SHY]",
                                                "suitability:max_equity", f"suitability:max_stress_loss[{SCENARIO}]"}


def test_holding_above_max_r_level_is_rejected():
    review = RiskEngine().review(proposal({"BIL": 0.4, "AGG": 0.6}), assets(), limits(), max_r_level=1)
    assert not review.approved
    assert review.violations == ["AGG (60.0%) is R2; investor maximum is R1"]


def test_unrated_holding_and_missing_max_r_level_fail_closed():
    unrated = [*assets(), AssetInfo(asset_id="EQ_GLOBAL", asset_class="equity")]
    review = RiskEngine().review(proposal({"BIL": 0.9, "EQ_GLOBAL": 0.1}), unrated, limits(0.5), max_r_level=5)
    assert any("no R-level in Agent C's catalog" in v for v in review.violations)
    review = RiskEngine().review(proposal({"BIL": 1.0}), assets(), limits(), max_r_level=None)
    assert review.violations == ["investor max_r_level not provided"]


def test_equity_cap_breach_is_rejected():
    review = RiskEngine().review(proposal({"BIL": 0.5, "VT": 0.5}), assets(), limits(0.40, 0.50), max_r_level=5)
    assert not review.approved
    assert any(v.startswith("equity weight 50.00% vs limit 40.00%") for v in review.violations)


def test_class_cap_breach_is_rejected_and_other_classes_are_ignored():
    holdings = {"BIL": 0.10, "SHY": 0.40, "VT": 0.30, "IAU": 0.20}
    infos = assets() + [AssetInfo(asset_id="IAU", asset_class="commodity")]
    caps = [Constraint(type="max_class_weight", value=0.10, source="risk_class", asset_class="commodity"),
            Constraint(type="max_class_weight", value=0.05, source="risk_class", asset_class="crypto")]
    review = RiskEngine().review(proposal(holdings), infos, caps, max_r_level=5)
    assert not review.approved
    assert review.violations == ["commodity weight 20.00% vs limit 10.00% (risk_class:max_class_weight[commodity])"]


def test_commodity_and_crypto_take_their_own_stress_shocks():
    infos = assets() + [AssetInfo(asset_id="IAU", asset_class="commodity"), AssetInfo(asset_id="IBIT", asset_class="crypto")]
    review = RiskEngine().review(proposal({"BIL": 0.5, "IAU": 0.4, "IBIT": 0.1}), infos,
                                 [Constraint(type="max_stress_loss", value=0.11, source="risk_class", scenario=SCENARIO)],
                                 max_r_level=5)
    # 40% x -15% (commodity) + 10% x -60% (crypto) = -12% > 11% limit
    assert not review.approved and "hypothetical loss 12.00%" in review.violations[0]


def test_single_stock_above_its_instrument_cap_is_rejected():
    infos = assets() + [AssetInfo(asset_id="AAPL", asset_class="equity", max_weight=0.10)]
    review = RiskEngine().review(proposal({"BIL": 0.85, "AAPL": 0.15}), infos, [], max_r_level=5)
    assert not review.approved
    assert "AAPL weight 15.00% vs instrument cap 10.00%" in review.violations
    ok = RiskEngine().review(proposal({"BIL": 0.90, "AAPL": 0.10}), infos, [], max_r_level=5)
    assert ok.approved, ok.violations


def test_stress_loss_breach_is_rejected():
    # SHY falls 5% in the scenario: 100% SHY loses 5% > 4%
    review = RiskEngine().review(proposal({"SHY": 1.0}), assets(), limits(), max_r_level=1)
    assert not review.approved
    assert any(v.startswith("hypothetical loss 5.00%") for v in review.violations)


def test_soft_constraints_and_other_types_are_not_enforced():
    soft = Constraint(type="max_equity", value=0.0, source="behavioral", hard=False)
    review = RiskEngine().review(proposal({"BIL": 0.5, "VT": 0.5}), assets(), [soft], max_r_level=5)
    assert review.approved


def test_empty_or_unnormalized_proposals_are_rejected():
    empty = PortfolioProposal(stage="constrained", method="test", as_of=AS_OF, status="infeasible", weights={})
    assert not RiskEngine().review(empty, assets(), limits(), max_r_level=1).approved
    assert not RiskEngine().review(proposal({"BIL": 0.5}), assets(), limits(), max_r_level=1).approved


def test_unknown_scenario_is_rejected():
    bad = Constraint(type="max_stress_loss", value=0.1, source="suitability", scenario="no_such_scenario")
    review = RiskEngine().review(proposal({"BIL": 1.0}), assets(), [bad], max_r_level=1)
    assert review.violations == ["unknown stress scenario 'no_such_scenario'"]


def test_engine_matches_agent_a_stress_numbers():
    """Independent arithmetic, same scenario definitions: it must agree with Agent A's reported stress."""
    profile = c1_by_capacity_profile()
    _, result, asset_infos = run_pipeline(profile)
    engine = RiskEngine()
    info = {a.asset_id: a for a in asset_infos}
    for name, reported in result.final.stress.items():
        recomputed = sum(w * engine._asset_shock(info[a], engine.scenarios[name])
                         for a, w in result.final.weights.items())
        assert recomputed == pytest.approx(reported, abs=1e-9)
