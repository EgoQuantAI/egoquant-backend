"""Goal-based path: Agent A's candidates -> Agent B's shortlist -> Monte Carlo -> chosen portfolio.

Run with:  pytest -v tests/system/test_goal_based.py      (SYNTHETIC prices; no internet needed)
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.behavioral import BehavioralTwinAgent, NoBehavioralData
from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource
from egoquant.agents.quant import QuantAgent
from egoquant.agents.quant import core as qc
from egoquant.agents.quant import goal as gq
from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.demo.synthetic_investors import synthetic_behavioral_provider
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES, AgentConfig, InvestorContext
from egoquant.pipeline import run_pipeline
from egoquant.risk_engine import RiskEngine
from egoquant.risk_profile import build_risk_profile, to_agent_c_limits, to_investor_context_limits
from tests.helpers import ASSESSED_AT, BANK, CONFIG, make_answers

AS_OF = date(2026, 9, 30)
R_LEVELS = RiskEngine().r_levels
CLASS_SCORES = {"C1": 15, "C2": 20, "C3": 25, "C4": 30, "C5": 40}
FAST = AgentConfig(run_sensitivity=False, goal_paths=2_000)


def goal(answers, multiple=1.4, monthly=0.0, years=10.0):
    return gq.GoalSpec(start_amount=answers.amount_invested, monthly_flow=monthly,
                       goal_amount=answers.amount_invested * multiple, horizon_years=years)


def run(answers, **kw):
    kw.setdefault("goal", goal(answers))
    return run_pipeline(answers, investor_id=kw.pop("investor_id", "t"), interest_areas=kw.pop("areas", ["stocks", "bonds", "gold"]),
                        goal_type="growth", horizon_years=10, as_of=AS_OF, price_source=SyntheticPriceSource(),
                        agent_config=kw.pop("agent_config", FAST), min_cash_buffer=0.05, **kw)


def candidate_menu(c_level="C3", areas=("stocks", "bonds", "gold")):
    profile = build_risk_profile(make_answers(gl_score=CLASS_SCORES[c_level]), BANK, CONFIG, assessed_at=ASSESSED_AT)
    interests = InvestorInterests(investor_id="t", interest_areas=list(areas), **to_agent_c_limits(profile, CONFIG))
    agent_c = MarketIntelligenceAgent(interests, source=SyntheticPriceSource())
    interest_assets = [i.ticker for i in agent_c.selection.instruments if any(r.startswith("interest:") for r in i.reasons)]
    context = InvestorContext(investor_id="t", risk_class=profile.c_level, goal_type="growth", horizon_years=10,
                              interest_assets=interest_assets, **to_investor_context_limits(profile, CONFIG, agent_c.selection))
    agent = QuantAgent(agent_c, NoBehavioralEvidence(), FAST)
    model = agent.prepare_market_model(context, AS_OF)
    return agent, context, model, agent.build_candidates(context, model)


# ==========================================================================
# 1. Solver options
# ==========================================================================
def _toy():
    cov = np.array([[0.04, 0.035, 0.0], [0.035, 0.04, 0.0], [0.0, 0.0, 0.0004]])
    return dict(cov=cov, asset_ids=["A", "B", "C"], upper_bounds=np.ones(3), bound_labels=["x"] * 3, linear_rows=[],
                vol_limits=[], equity_mask=np.array([1.0, 1.0, 0.0]))


def test_ridge_spreads_weight_over_similar_assets():
    plain = qc.solve_portfolio(**_toy(), equity_target=0.5)
    spread = qc.solve_portfolio(**_toy(), equity_target=0.5, ridge=0.04)
    assert spread.weights is not None and abs(spread.weights.sum() - 1) < 1e-6
    assert abs(spread.weights[0] - spread.weights[1]) <= abs(plain.weights[0] - plain.weights[1]) + 1e-6
    assert spread.weights[0] > 0.2 and spread.weights[1] > 0.2


def test_target_tracking_finds_the_nearest_feasible_mix():
    toy = _toy()
    toy["upper_bounds"] = np.array([0.3, 1.0, 1.0])          # A is capped below its target
    result = qc.solve_portfolio(**toy, equity_target=0.6, target_weights=np.array([0.5, 0.1, 0.4]))
    assert result.weights[0] == pytest.approx(0.3, abs=1e-4) and result.weights[1] == pytest.approx(0.3, abs=1e-4)


# ==========================================================================
# 2. Simulation
# ==========================================================================
def _history(days=600, daily=0.0003, vol=0.01, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=days)
    return pd.DataFrame({"RISKY": rng.normal(daily, vol, days), "CASH": np.full(days, 0.0001)}, index=idx)


def _analyze(spec, history=None, **kw):
    history = _history() if history is None else history
    candidates = [(0, "safe", 0.0, {"CASH": 1.0}), (1, "mixed", 0.5, {"RISKY": 0.5, "CASH": 0.5}),
                  (2, "risky", 1.0, {"RISKY": 1.0})]
    return gq.analyze_goal(history, candidates, {"RISKY": 0.001, "CASH": 0.0}, {0: 0.0, 1: 0.08, 2: 0.16}, spec,
                           n_paths=kw.get("n_paths", 3000), block_days=21, seed=kw.get("seed", 7), tie=0.01)


def test_simulation_is_deterministic_and_well_formed():
    spec = gq.GoalSpec(start_amount=10_000, monthly_flow=100, goal_amount=14_000, horizon_years=5)
    a, b = _analyze(spec), _analyze(spec)
    assert a.model_dump() == b.model_dump()
    for o in a.outcomes:
        assert 0 <= o.prob_goal <= 1 and o.p10 <= o.p50 <= o.p90 and o.worst5_shortfall >= 0
        assert 0 <= o.drawdown_p50 <= o.drawdown_p90 <= 1 and 0 <= o.prob_drawdown_20 <= 1
    by_index = {o.menu_index: o for o in a.outcomes}
    assert by_index[0].drawdown_p90 < 0.01 < by_index[2].drawdown_p90        # cash barely falls, risky does
    assert [f.year for f in a.fan][0] == 0 and a.fan[-1].year == 5
    assert all(f.p10 <= f.p25 <= f.p50 <= f.p75 <= f.p90 for f in a.fan)
    assert a.fan[0].p50 == pytest.approx(10_000)
    assert a.total_contributed == pytest.approx(10_000 + 100 * 60)


def test_an_easy_goal_goes_to_the_least_risky_way_of_reaching_it():
    easy = _analyze(gq.GoalSpec(start_amount=10_000, monthly_flow=0, goal_amount=9_000, horizon_years=3))
    assert easy.outcomes[0].prob_goal == pytest.approx(1.0)
    assert easy.chosen_index == 0                              # cash already reaches it, with the least risk


def test_a_growth_goal_prefers_the_portfolio_that_can_reach_it():
    hard = _analyze(gq.GoalSpec(start_amount=10_000, monthly_flow=0, goal_amount=13_000, horizon_years=8))
    by_index = {o.menu_index: o for o in hard.outcomes}
    assert by_index[0].prob_goal == 0.0                         # cash cannot grow 30%
    assert hard.chosen_index != 0 and hard.outcomes[0].prob_goal >= by_index[1].prob_goal


def test_withdrawals_can_run_the_money_out():
    spec = gq.GoalSpec(start_amount=10_000, monthly_flow=-500, goal_amount=1, horizon_years=3)
    result = _analyze(spec)
    assert all(o.prob_depleted > 0.5 for o in result.outcomes)


# ==========================================================================
# 3. Agent A: candidates
# ==========================================================================
@pytest.mark.parametrize("c_level", ["C2", "C3", "C4", "C5"])
def test_candidates_are_distinct_and_inside_every_limit(c_level):
    agent, context, model, menu = candidate_menu(c_level)
    policy = DEFAULT_RISK_CLASS_POLICIES[c_level]
    assert 1 <= len(menu.portfolios) <= 100
    weights = [np.array([p.weights.get(a, 0.0) for a in model.asset_ids]) for p in menu.portfolios]
    for i, a in enumerate(weights):
        for b in weights[i + 1:]:
            assert np.max(np.abs(a - b)) > agent.config.candidate_min_distance
    for p in menu.portfolios:
        assert not p.violated_constraints
        assert (p.equity_weight or 0) <= policy.max_equity + 1e-6
        assert -p.stress["equity_down_30_bond_down_5"] <= policy.max_stress_loss + 1e-6
        assert all(R_LEVELS[a] <= int(c_level[1]) for a, w in p.weights.items() if w > 1e-6)


def test_c3_gets_100_candidates_in_five_styles():
    _, _, _, menu = candidate_menu("C3")
    assert len(menu.portfolios) == 100
    assert {p.method for p in menu.portfolios} == {"min_variance", "diversified", "equal_weight", "inverse_vol", "interest_tilt"}


# ==========================================================================
# 4. Agent B: shortlist
# ==========================================================================
def test_screening_keeps_5_to_20_inside_the_comfort_band():
    _, context, _, menu = candidate_menu("C4")
    result = BehavioralTwinAgent(NoBehavioralData()).screen_candidates("t", menu, 0.5)
    assert 5 <= len(result.approved) <= 20
    assert (result.band_lower, result.band_upper) == pytest.approx((DEFAULT_RISK_CLASS_POLICIES["C3"].max_stress_loss,
                                                                    DEFAULT_RISK_CLASS_POLICIES["C4"].max_stress_loss))
    assert set(result.approved).isdisjoint(result.rejected)
    assert result.counts["approved"] == len(result.approved)


def test_a_cautious_game_tightens_the_band_and_removes_candidates():
    _, context, _, menu = candidate_menu("C4")
    no_evidence = BehavioralTwinAgent(NoBehavioralData()).screen_candidates("newcomer", menu, 0.5)
    newcomer = BehavioralTwinAgent(synthetic_behavioral_provider(seed=11)).screen_candidates("newcomer", menu, 0.5)
    assert newcomer.band_upper < no_evidence.band_upper                     # game choice 1: C1's limit
    assert all(-menu.portfolios[i].stress["equity_down_30_bond_down_5"] <= newcomer.band_upper + 1e-6
               for i in newcomer.approved)


# ==========================================================================
# 5. Whole pipeline
# ==========================================================================
@pytest.mark.parametrize("c_level", list(CLASS_SCORES))
def test_goal_path_chooses_an_approved_shortlisted_portfolio(c_level):
    answers = make_answers(gl_score=CLASS_SCORES[c_level])
    out = run(answers)
    assert out.approved, out.review.violations
    assert out.goal_analysis.chosen_index in out.screening.approved
    assert out.result.final.method.startswith("goal_based:")
    chosen = out.result.menu.portfolios[out.goal_analysis.chosen_index]
    assert out.result.final.weights == chosen.weights                       # the proposal IS the chosen candidate
    best = max(o.prob_goal for o in out.goal_analysis.outcomes)
    assert out.goal_analysis.outcomes[0].prob_goal >= best - FAST.goal_tie_tolerance - 1e-9
    assert any("chance of reaching your goal" in r.text for r in out.report.reasons)
    assert not any(r.text.startswith("We lowered the share in stocks") for r in out.report.reasons)


def test_goal_path_uses_up_to_ten_years_of_history():
    out = run(make_answers(gl_score=30))
    assert out.goal_analysis.history_days > 2000                           # synthetic history is long enough


def test_without_a_goal_the_original_path_runs():
    out = run(make_answers(gl_score=25), goal=None)
    assert out.goal_analysis is None and out.screening is None
    assert not out.result.final.method.startswith("goal_based")


def test_the_same_inputs_give_the_same_choice():
    a, b = run(make_answers(gl_score=30)), run(make_answers(gl_score=30))
    assert a.goal_analysis.model_dump() == b.goal_analysis.model_dump()
    assert a.result.final.weights == b.result.final.weights
