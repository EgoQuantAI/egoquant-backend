"""Tests for Agent B (Behavioral Twin). Run with:  pytest -v

Layout:  1) math core   2) limits sent to Agent A, end to end with the real Agent A
         3) the Advisor-only patterns   4) data handling and edge cases
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.behavioral import core as bc
from egoquant.agents.behavioral import BehavioralTwinAgent
from egoquant.agents.quant import QuantAgent
from egoquant.agents.behavioral.models import BehavioralData, BehaviorConfig, GameResult, TradingHistory
from egoquant.demo.synthetic_investors import (
    SYNTHETIC_PROFILES,
    InMemoryBehavioralDataProvider,
    InvestorPolicy,
    build_synthetic_data,
    eckel_grossman,
    simulate_history,
    synthetic_history_returns,
)
from egoquant.demo.stubs import SyntheticMarketAgent
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES, AgentConfig, InvestorContext, RiskClassPolicy

AS_OF = date(2026, 9, 30)
SCENARIO = "equity_down_30_bond_down_5"


# ==========================================================================
# Shared fixtures and helpers
# ==========================================================================
@pytest.fixture(scope="module")
def synthetic():
    return build_synthetic_data(seed=11)


@pytest.fixture(scope="module")
def returns():
    return synthetic_history_returns(seed=11)


def make_context(investor_id: str, risk_class: str = "C4", **overrides) -> InvestorContext:
    base = dict(investor_id=investor_id, risk_class=risk_class, goal_type="growth", horizon_years=10,
                liquidity_need=0.05, min_cash_buffer=0.05, max_acceptable_loss=0.25,
                suitability_max_equity=0.80, max_asset_weight=0.40)
    base.update(overrides)
    return InvestorContext(**base)


def run_pair(data: dict, investor_id: str, config: BehaviorConfig | None = None, **context):
    """Run the real Agent A with the real Agent B; return (A result, B report for the same menu)."""
    if investor_id in SYNTHETIC_PROFILES:
        profile = SYNTHETIC_PROFILES[investor_id]
        context = {"risk_class": profile.risk_class, **profile.context_overrides, **context}
    b = BehavioralTwinAgent(InMemoryBehavioralDataProvider(data), config)
    a = QuantAgent(SyntheticMarketAgent(seed=7), b, AgentConfig())
    result = a.run(make_context(investor_id, **context), AS_OF)
    return result, b.analyze(investor_id, result.menu)


def with_data(base: BehavioralData, **update) -> dict:
    data = base.model_copy(update=update)
    return {data.investor_id: data}


def feature(report, name):
    return next(f for f in report.features if f.name == name)


# ==========================================================================
# 1. Math core
# ==========================================================================
def test_twr_removes_deposits_and_withdrawals():
    dates = pd.bdate_range("2026-01-05", periods=3)
    value = pd.Series([100.0, 210.0, 105.0], index=dates)
    flows = pd.Series([100.0, -100.0], index=dates[1:])
    r = bc.time_weighted_returns(value, flows)
    assert r.tolist() == pytest.approx([0.0, 0.10, -0.0238095238])   # (210-100)/100-1, (105+100)/210-1


def test_rolling_drawdown_and_spells():
    index = pd.Series([1.0, 1.1, 0.99, 0.88, 1.12, 1.0])
    dd = bc.rolling_drawdown(index, window=10).to_numpy()
    assert dd[3] == pytest.approx(0.2) and dd[4] == 0.0
    assert bc.drawdown_spells(dd, 0.05) == [(2, 3), (5, 5)]


def _episode_arrays(sell_day: int, sell_value: float, outflow: float = 0.0):
    dd = np.array([0.0, 0.02, 0.06, 0.12, 0.15, 0.10, 0.04, 0.0])
    risky = np.full(8, 1000.0)
    net, gross, out = np.zeros(8), np.zeros(8), np.zeros(8)
    net[sell_day] = gross[sell_day] = sell_value
    out[sell_day] = outflow
    return dd, risky, net, gross, out


def _detect(dd, risky, net, gross, out):
    return bc.detect_episodes(dd, risky, net, gross, out, spell_floor=0.03, min_trigger_drawdown=0.05,
                              min_sell_fraction=0.30, sell_window_days=3, max_withdrawal_share=0.5,
                              min_informative_depth=0.08)


def test_capitulation_is_detected_at_the_loss_where_selling_started():
    episodes, held = _detect(*_episode_arrays(sell_day=3, sell_value=600.0))
    assert len(episodes) == 1 and held == []
    assert episodes[0].trigger_drawdown == pytest.approx(0.12)
    assert episodes[0].sold_fraction == pytest.approx(0.6)
    assert episodes[0].spell_depth == pytest.approx(0.15)


def test_small_sale_is_not_capitulation_and_spell_counts_as_held_through():
    episodes, held = _detect(*_episode_arrays(sell_day=3, sell_value=100.0))
    assert episodes == [] and len(held) == 1 and held[0].depth == pytest.approx(0.15)


def test_selling_to_fund_a_withdrawal_is_not_capitulation():
    episodes, _ = _detect(*_episode_arrays(sell_day=3, sell_value=600.0, outflow=600.0))
    assert episodes == []


def test_selling_before_a_real_loss_is_not_capitulation():
    episodes, _ = _detect(*_episode_arrays(sell_day=1, sell_value=600.0))   # dd 2%: below the trigger
    assert episodes == []


def test_panic_cost_compares_holding_with_what_happened():
    # Half cash (no price), half equity that rises 20%; the real account only grew 2%.
    cost = bc.panic_cost(np.array([50.0, 50.0]), np.array([np.nan, 100.0]), np.array([np.nan, 120.0]), 1.02)
    assert cost == pytest.approx(1.10 - 1.02)


def test_disposition_counts_follow_odean():
    # Two stocks bought at 100. On day 2, A is at 110 and is sold; B is at 90 and is kept.
    prices = np.array([[100.0, 100.0], [105.0, 95.0], [110.0, 90.0]])
    counts = bc.disposition_counts(prices, np.array([0.0, 0.0]),
                                   [(0, 0, "buy", 100.0), (0, 1, "buy", 100.0), (2, 0, "sell", 50.0)])
    assert (counts.realized_gains, counts.paper_losses, counts.sell_days) == (1, 1, 1)
    assert counts.pgr == 1.0 and counts.plr == 0.0 and counts.effect == 1.0


def test_holding_period_and_chase_share():
    assert bc.implied_holding_years(100.0, 50.0, 252) == pytest.approx(2.0)
    assert bc.implied_holding_years(100.0, 0.0, 252) is None
    assert bc.chase_share(np.array([10.0, 30.0]), np.array([True, False])) == pytest.approx(0.25)
    assert bc.chase_share(np.array([]), np.array([], dtype=bool)) is None


def test_diagnostics_helpers():
    assert bc.implied_risk_aversion(0.4, 0.05, 0.16) == pytest.approx(4.8828125)
    assert bc.implied_risk_aversion(0.0, 0.05, 0.16) is None
    caps = [(k, DEFAULT_RISK_CLASS_POLICIES[k].max_equity) for k in ["C1", "C2", "C3", "C4", "C5"]]
    # C1 and C2 both cap equity at 0%, so any equity share maps past C2 (questionnaire table)
    assert [bc.class_for_equity_share(s, caps) for s in (0.0, 0.25, 0.55, 0.85, 0.95)] == \
        ["C1", "C3", "C4", "C5", "C5"]
    assert bc.ceil_to(0.04751, 1e-4) == pytest.approx(0.0476)
    assert bc.annual_turnover(100.0, 100.0, 252) == pytest.approx(0.5)


# ==========================================================================
# 2. Limits sent to Agent A (end to end with the real Agent A)
# ==========================================================================
def test_two_consistent_panics_give_a_hard_limit_that_agent_a_enforces(synthetic):
    result, report = run_pair(synthetic, "panic_seller")
    caps = [e for e in report.evidence if e.kind == "capitulation"]
    assert len(caps) == 2 and all(0.10 <= e.drawdown <= 0.14 for e in caps)
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert limit.hard and limit.confidence == pytest.approx(0.8) and limit.scenario == SCENARIO
    assert limit.value == pytest.approx(report.capitulation_threshold)
    final = result.final
    assert final.status == "optimal" and final.ignored_constraints == [] and final.relaxed_constraints == []
    assert -final.stress[SCENARIO] <= limit.value + 1e-6
    assert final.equity_weight < result.menu.target_equity                   # B pulled equity down


def test_sara_one_panic_confirmed_by_the_game_is_soft_at_0_7(synthetic):
    result, report = run_pair(synthetic, "sara")
    assert "single_panic_confirmed_by_game" in report.flags
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert not limit.hard and limit.confidence == pytest.approx(0.7)
    assert result.final.relaxed_constraints == [] and result.final.ignored_constraints == []
    assert -result.final.stress[SCENARIO] <= limit.value + 1e-6


def test_one_panic_without_game_support_stays_at_0_6(synthetic):
    data = with_data(synthetic["sara"], game_result=eckel_grossman(4))       # game NOT more cautious than C3
    _, report = run_pair(data, "sara")
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert not limit.hard and limit.confidence == pytest.approx(0.6)


def test_contradicted_panic_is_reported_but_agent_a_ignores_it(returns):
    # Panics once in the 2022 crash, then holds through a deeper fall in 2025.
    history = simulate_history(InvestorPolicy(
        target={"EQ_GLOBAL": 0.45, "EQ_EMERGING": 0.25, "BOND_GOVT": 0.25, "CASH_MM": 0.05},
        panic_thresholds=(0.11,), reentry_after_days=60), returns)
    data = {"mixed": BehavioralData(investor_id="mixed", history=history, data_source="synthetic")}
    result, report = run_pair(data, "mixed")
    assert "panic_evidence_conflicts_with_held_through" in report.flags
    assert report.constraints and all(c.confidence == pytest.approx(0.45) for c in report.constraints)
    assert sorted(result.final.ignored_constraints) == sorted(c.label for c in report.constraints)


def test_game_alone_2_levels_below_class_uses_agent_a_class_limit_at_most_one_class_down(synthetic):
    result, report = run_pair(synthetic, "newcomer")                          # C4, game choice 1 -> level 1
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert not limit.hard and limit.confidence == pytest.approx(0.55)
    # Level 1 would be C1's limit; game results alone lower the limit by at most one class: C3's.
    assert limit.value == pytest.approx(DEFAULT_RISK_CLASS_POLICIES["C3"].max_stress_loss)
    assert "game_limit_capped_one_class" in report.flags
    assert any("capped at C3's limit" in f for f in report.advisor_facts)
    assert report.threshold_source == "game" and report.capitulation_threshold is None
    assert -result.final.stress[SCENARIO] <= limit.value + 1e-6 and result.final.relaxed_constraints == []


def test_game_after_as_of_is_ignored_unless_live_advice_allows_it(synthetic):
    later = with_data(synthetic["newcomer"], game_result=eckel_grossman(1, played="2026-10-01T09:00:00"))
    _, strict = run_pair(later, "newcomer")                                  # AS_OF is 2026-09-30
    assert "game_after_as_of_ignored" in strict.flags and strict.constraints == []
    _, live = run_pair(later, "newcomer", BehaviorConfig(accept_game_after_as_of=True))
    assert "game_after_as_of_ignored" not in live.flags
    assert any(c.type == "max_stress_loss" for c in live.constraints)


def test_game_one_level_below_class_is_only_a_flag(synthetic):
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(3))  # C4 vs level 3
    _, report = run_pair(data, "newcomer")
    assert report.constraints == [] and "game_slightly_more_cautious" in report.flags


def test_aggressive_game_never_loosens_anything(synthetic):
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(6))  # C4 vs level 5
    result, report = run_pair(data, "newcomer")
    assert report.constraints == [] and "game_more_aggressive_than_class" in report.flags
    assert result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_equity + 1e-6


def test_real_history_outranks_the_game(synthetic):
    data = with_data(synthetic["steady"], game_result=eckel_grossman(1))    # held through crashes
    _, report = run_pair(data, "steady")
    assert report.constraints == [] and "game_overridden_by_trading_history" in report.flags


def test_overtrader_gets_a_soft_20pct_turnover_limit_that_agent_a_meets(synthetic):
    result, report = run_pair(synthetic, "overtrader")
    limit = next(c for c in report.constraints if c.type == "max_turnover")
    assert not limit.hard and limit.value == pytest.approx(0.20)
    final = result.final
    assert final.status == "optimal" and final.relaxed_constraints == []
    assert final.implementation.turnover <= 0.20 + 1e-6
    assert final.equity_weight == pytest.approx(result.menu.target_equity, abs=1e-6)   # no equity cost


def test_turnover_limit_needs_current_holdings(synthetic):
    result, report = run_pair(synthetic, "overtrader", current_holdings=None)
    assert "turnover_limit_needs_current_holdings" in report.flags
    assert all(c.type != "max_turnover" for c in report.constraints)


def test_turnover_limit_is_widened_so_a_loss_limit_survives(synthetic):
    # The panic-seller also counts as an overtrader under a lower threshold: both limits are sent.
    holdings = {"EQ_EMERGING": 0.60, "BOND_GOVT": 0.30, "CASH_MM": 0.10}
    result, report = run_pair(synthetic, "panic_seller", BehaviorConfig(overtrading_turnover=0.2),
                              current_holdings=holdings)
    loss = next(c for c in report.constraints if c.type == "max_stress_loss")
    turnover = next(c for c in report.constraints if c.type == "max_turnover")
    assert "turnover_limit_widened_for_loss_limit" in report.flags and turnover.value > 0.20
    final = result.final
    assert final.relaxed_constraints == [] and final.status == "optimal"
    assert -final.stress[SCENARIO] <= loss.value + 1e-6
    assert final.implementation.turnover <= turnover.value + 1e-6


def test_b_only_tightens(synthetic):
    for investor_id, profile in SYNTHETIC_PROFILES.items():
        result, report = run_pair(synthetic, investor_id)
        class_limit = DEFAULT_RISK_CLASS_POLICIES[profile.risk_class].max_stress_loss
        for c in report.constraints:
            assert c.source == "behavioral" and c.type in {"max_stress_loss", "max_turnover"}
            if c.type == "max_stress_loss":
                assert c.value <= class_limit
        assert result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES[profile.risk_class].max_equity + 1e-6


# ==========================================================================
# 3. Advisor-only patterns (flags, never limits)
# ==========================================================================
def test_cost_of_panic_is_measured_and_flagged(synthetic):
    _, report = run_pair(synthetic, "panic_seller")
    cost = feature(report, "cost_of_panic")
    assert cost.value > 0.02 and cost.n_evidence == 2 and "panic_selling_was_costly" in report.flags


def test_disposition_effect_is_found_only_where_it_exists(synthetic):
    _, disp = run_pair(synthetic, "disposition")
    assert feature(disp, "disposition_effect").value > 0.10 and "disposition_effect" in disp.flags
    _, churn = run_pair(synthetic, "overtrader")                             # sells winners and losers alike
    assert abs(feature(churn, "disposition_effect").value) < 0.10 and "disposition_effect" not in churn.flags


def test_return_chasing_and_hype_chasing(synthetic):
    _, chaser = run_pair(synthetic, "hype_chaser")
    assert feature(chaser, "return_chasing_share").value >= 0.40 and "return_chasing" in chaser.flags
    assert "hype_chasing" in chaser.flags
    hype = [e for e in chaser.evidence if e.kind == "hype_buy"]
    assert hype and all(e.asset_id == "MEME_CO" for e in hype)
    _, churn = run_pair(synthetic, "overtrader")                             # buys on a timer, not after rallies
    assert feature(churn, "return_chasing_share").value < 0.40 and "return_chasing" not in churn.flags


def test_horizon_mismatch(synthetic):
    _, churn = run_pair(synthetic, "overtrader")
    assert feature(churn, "implied_holding_years").value < 1.0 and "horizon_mismatch" in churn.flags
    _, steady = run_pair(synthetic, "steady")                                # never sells equity
    assert feature(steady, "implied_holding_years").value is None and "horizon_mismatch" not in steady.flags


def test_patterns_need_prices_and_degrade_gracefully(synthetic):
    base = synthetic["hype_chaser"]
    data = with_data(base, history=base.history.model_copy(update={"asset_prices": None}))
    _, report = run_pair(data, "hype_chaser")
    for name in ("disposition_effect", "return_chasing_share", "hype_buy_share"):
        assert feature(report, name).value is None and "price" in feature(report, name).note
    assert report.status == "ok"


def test_advisor_facts_point_to_evidence(synthetic):
    _, report = run_pair(synthetic, "panic_seller")
    assert any("Sold 69% of equities" in f for f in report.advisor_facts)
    assert any("behind simply holding on" in f for f in report.advisor_facts)


# ==========================================================================
# 4. Data handling and edge cases
# ==========================================================================
def test_thin_history_without_game_is_insufficient_data(synthetic):
    result, report = run_pair(synthetic, "thin_history")
    assert report.status == "insufficient_data" and report.constraints == []
    assert all(a.fit == "no_evidence" for a in report.menu_assessment)
    assert result.final.status in {"optimal", "fallback"}


def test_unknown_investor_is_insufficient_data_not_an_error(synthetic):
    result, report = run_pair(synthetic, "nobody")
    assert report.status == "insufficient_data" and report.data_source == "none"
    assert result.behavioral_constraints == []


def test_history_after_as_of_is_ignored(synthetic):
    real = synthetic["panic_seller"]
    h = real.history
    cut = pd.Timestamp(AS_OF) - pd.tseries.offsets.BDay(200)
    early = h.model_copy(update={
        "positions": h.positions.loc[:cut], "external_flows": h.external_flows.loc[:cut],
        "trades": [t for t in h.trades if pd.Timestamp(t.trade_date) <= cut],
        "asset_prices": h.asset_prices.loc[:cut],
    })
    b_full = BehavioralTwinAgent(InMemoryBehavioralDataProvider({"panic_seller": real}))
    b_early = BehavioralTwinAgent(InMemoryBehavioralDataProvider(with_data(real, history=early)))
    a = QuantAgent(SyntheticMarketAgent(seed=7), b_full, AgentConfig())
    context = make_context("panic_seller")
    menu = a.build_candidate_menu(context, a.prepare_market_model(context, AS_OF))
    menu_early = menu.model_copy(update={"as_of": cut.date()})
    full, early_report = b_full.analyze("panic_seller", menu_early), b_early.analyze("panic_seller", menu_early)
    assert "history_truncated_at_as_of" in full.flags
    assert full.capitulation_threshold == early_report.capitulation_threshold
    assert [c.value for c in full.constraints] == [c.value for c in early_report.constraints]


def test_game_played_after_as_of_is_ignored(synthetic):
    data = with_data(synthetic["newcomer"], game_result=eckel_grossman(1, "2027-01-01T09:00:00"))
    _, report = run_pair(data, "newcomer")
    assert "game_after_as_of_ignored" in report.flags and report.constraints == []


def test_bad_data_gives_invalid_data_status_and_no_constraints(synthetic):
    real = synthetic["panic_seller"]
    broken = real.history.positions.copy()
    broken.iloc[5, 0] = np.nan
    result, report = run_pair(with_data(real, history=real.history.model_copy(update={"positions": broken})),
                              "panic_seller")
    assert report.status == "invalid_data" and report.constraints == [] and report.issues
    assert result.behavioral_constraints == []


def test_history_without_cash_column_is_rejected():
    positions = pd.DataFrame({"EQ": np.full(200, 100.0)}, index=pd.bdate_range("2025-01-01", periods=200))
    data = {"nocash": BehavioralData(investor_id="nocash", data_source="synthetic",
                                     history=TradingHistory(positions=positions, asset_classes={"EQ": "equity"}))}
    _, report = run_pair(data, "nocash")
    assert report.status == "invalid_data" and any("cash column" in i for i in report.issues)


def test_negative_prices_are_rejected(synthetic):
    real = synthetic["hype_chaser"]
    prices = real.history.asset_prices.copy()
    prices.iloc[3, 1] = -1.0
    _, report = run_pair(with_data(real, history=real.history.model_copy(update={"asset_prices": prices})),
                         "hype_chaser")
    assert report.status == "invalid_data"


def test_unreachable_limit_is_clamped_to_safest_menu_portfolio(synthetic):
    policies = dict(DEFAULT_RISK_CLASS_POLICIES)
    policies["C1"] = RiskClassPolicy(risk_class="C1", max_equity=0.10, target_equity=0.05, max_vol=0.04,
                                     max_stress_loss=0.001)
    config = BehaviorConfig(risk_class_policies=policies, game_max_class_drop=3)   # let the game reach C1 here
    result, report = run_pair(synthetic, "newcomer", config)
    assert "behavioral_limit_clamped_to_menu" in report.flags
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert limit.value >= min(a.scenario_loss for a in report.menu_assessment)
    assert result.final.status != "infeasible" and result.final.relaxed_constraints == []


def test_unknown_scenario_is_flagged_and_nothing_is_sent(synthetic):
    _, report = run_pair(synthetic, "panic_seller", BehaviorConfig(stress_scenario="does_not_exist"))
    assert "stress_scenario_missing_from_menu" in report.flags
    assert all(c.type != "max_stress_loss" for c in report.constraints)


def test_reports_are_deterministic_and_hash_tracks_config(synthetic):
    _, first = run_pair(synthetic, "panic_seller")
    _, second = run_pair(synthetic, "panic_seller")
    assert first.model_dump_json() == second.model_dump_json()
    _, other = run_pair(synthetic, "panic_seller", BehaviorConfig(min_sell_fraction=0.5))
    assert other.input_hash != first.input_hash


def test_report_is_json_serializable_for_the_advisor(synthetic):
    _, report = run_pair(synthetic, "hype_chaser")
    text = report.model_dump_json()
    assert '"advisor_facts"' in text and '"menu_assessment"' in text


def test_config_and_game_validation():
    with pytest.raises(ValueError):
        BehaviorConfig(min_trigger_drawdown=0.01, spell_floor=0.03)
    with pytest.raises(ValueError):
        BehaviorConfig(game_level_map={1: 1, 2: 2})
    with pytest.raises(ValueError):
        GameResult(choice=7, payoff_low=0, payoff_high=1, version="v", timestamp="2026-09-20T10:00:00")
    with pytest.raises(ValueError):
        eckel_grossman(2, "yesterday")
