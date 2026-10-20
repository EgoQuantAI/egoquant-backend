"""Agent A end to end on SYNTHETIC data stubs: risk classes, constraints, Agent B limits, regimes."""

from datetime import date

import numpy as np
import pytest

from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.agents.quant import DataValidationError, QuantAgent
from egoquant.agents.quant import core as qc
from egoquant.agents.quant.backtest import HistoricalMarketProvider
from egoquant.demo.stubs import StubBehavioralAgent, SyntheticMarketAgent
from egoquant.demo.synthetic_market import make_synthetic_history, synthetic_asset_infos
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES, AgentConfig, Constraint, InvestorContext, RiskClassPolicy

AS_OF = date(2026, 9, 30)


# ==========================================================================
# 3. Agent behavior
# ==========================================================================
def make_context(**overrides) -> InvestorContext:
    base = dict(investor_id="t1", risk_class="C3", goal_type="growth", horizon_years=10,
                min_cash_buffer=0.05, max_acceptable_loss=0.25, suitability_max_equity=0.80,
                max_asset_weight=0.40)
    base.update(overrides)
    return InvestorContext(**base)


def make_agent(behavior=None, **config) -> QuantAgent:
    return QuantAgent(SyntheticMarketAgent(seed=7), behavior or StubBehavioralAgent(), AgentConfig(**config))


def crisis_agent(crisis: bool, **config) -> QuantAgent:
    """Agent over a long synthetic history; optionally a volatility spike in the last 25 days."""
    prices = make_synthetic_history(seed=5, n_days=700, end_date=AS_OF, crisis_start=675 if crisis else None,
                                    crisis_len=25)
    provider = HistoricalMarketProvider(prices, synthetic_asset_infos())
    return QuantAgent(provider, NoBehavioralEvidence(), AgentConfig(run_sensitivity=False, **config))


# ---- risk class C1..C5 -----------------------------------------------------
def test_each_risk_class_respects_its_equity_cap_and_volatility_cap():
    policies = AgentConfig().risk_class_policies
    for risk_class, policy in policies.items():
        result = make_agent(StubBehavioralAgent(max_equity=1.0)).run(make_context(risk_class=risk_class), AS_OF)
        final = result.final
        assert final.status in {"optimal", "fallback"}, risk_class
        assert final.equity_weight <= policy.max_equity + 1e-6
        assert final.risk.ex_ante_vol <= policy.max_vol + 1e-6
        assert final.risk_class == risk_class


def test_risk_ladder_is_monotonic_from_c1_to_c5():
    equity, vol = [], []
    for risk_class in ["C1", "C2", "C3", "C4", "C5"]:
        final = make_agent(NoBehavioralEvidence()).run(make_context(risk_class=risk_class), AS_OF).final
        equity.append(final.equity_weight)
        vol.append(final.risk.ex_ante_vol)
    assert equity == sorted(equity) and vol == sorted(vol)
    assert equity[-1] > equity[0] + 0.5


def test_final_starts_at_the_class_target_equity():
    final = make_agent(NoBehavioralEvidence()).run(make_context(risk_class="C3"), AS_OF).final
    assert final.equity_weight == pytest.approx(0.40, abs=1e-6)


def test_menu_top_is_the_tightest_equity_cap_and_default_respects_the_class_target():
    agent = make_agent()
    context = make_context(risk_class="C4", suitability_max_equity=0.55)      # suitability tighter than C4's 70%
    menu = agent.build_candidate_menu(context, agent.prepare_market_model(context, AS_OF))
    equity = [p.equity_weight for p in menu.portfolios]
    assert equity == sorted(equity) and max(equity) <= 0.55 + 1e-6
    assert menu.target_equity == pytest.approx(0.55)                         # min(class target 60%, cap 55%)
    assert menu.portfolios[menu.default_index].equity_weight <= menu.target_equity + 1e-6
    assert menu.risk_class == "C4"


def test_stricter_suitability_cap_overrides_the_class_and_is_reported_binding():
    final = make_agent(NoBehavioralEvidence()).run(make_context(risk_class="C5", suitability_max_equity=0.30), AS_OF).final
    assert final.equity_weight == pytest.approx(0.30, abs=1e-6)
    assert "suitability:max_equity" in final.binding_constraints


def test_class_stress_loss_cap_holds_in_the_capacity_scenario():
    for risk_class, policy in AgentConfig().risk_class_policies.items():
        final = make_agent(NoBehavioralEvidence()).run(make_context(risk_class=risk_class, max_acceptable_loss=None), AS_OF).final
        assert final.stress["equity_down_30_bond_down_5"] >= -policy.max_stress_loss - 1e-6


def test_zero_acceptable_loss_is_a_limit_not_missing():
    """0 means 'no loss accepted' (the questionnaire's gate), not 'no limit'. Only assets that do not
    fall in the capacity scenario may be held."""
    context = make_context(risk_class="C1", max_acceptable_loss=0.0, max_asset_weight=1.0)
    final = make_agent(NoBehavioralEvidence()).run(context, AS_OF).final
    assert final.status != "infeasible"
    assert final.stress["equity_down_30_bond_down_5"] >= -1e-6
    assert "suitability:max_stress_loss[equity_down_30_bond_down_5]" in final.binding_constraints


def test_class_volatility_cap_is_enforced_when_it_binds():
    """Default caps are loose, so tighten C3 on purpose: the optimizer must cut equity to respect it."""
    policies = dict(AgentConfig().risk_class_policies)
    policies["C2"] = policies["C2"].model_copy(update={"max_vol": 0.04})
    policies["C3"] = policies["C3"].model_copy(update={"max_vol": 0.045})
    loose = make_agent(NoBehavioralEvidence()).run(make_context(risk_class="C3"), AS_OF).final
    tight = make_agent(NoBehavioralEvidence(), risk_class_policies=policies).run(make_context(risk_class="C3"), AS_OF)
    assert loose.risk.ex_ante_vol > 0.045                                    # the cap really would matter
    assert tight.final.risk.ex_ante_vol <= 0.045 + 1e-6
    assert tight.final.equity_weight < loose.equity_weight
    assert "lowered from 40%" in tight.final.message
    assert tight.menu.infeasible_levels or tight.menu.portfolios[-1].equity_weight < 0.50   # menu shows the squeeze


def test_class_equity_cap_sets_the_top_of_the_menu():
    agent, context = make_agent(), make_context(risk_class="C3", suitability_max_equity=0.80)   # class cap 40% < 80%
    menu = agent.build_candidate_menu(context, agent.prepare_market_model(context, AS_OF))
    assert max(p.equity_weight for p in menu.portfolios) == pytest.approx(0.40, abs=1e-6)
    assert menu.portfolios[-1].binding_constraints.count("risk_class:max_equity") == 1


def test_class_stress_loss_cap_is_enforced_when_it_binds():
    """Tighten C3's loss cap on purpose (12% vs about 15% at its normal equity level)."""
    policies = dict(AgentConfig().risk_class_policies)
    policies["C3"] = policies["C3"].model_copy(update={"max_stress_loss": 0.12})
    loose = make_agent(NoBehavioralEvidence()).run(make_context(risk_class="C3", max_acceptable_loss=None), AS_OF).final
    tight = make_agent(NoBehavioralEvidence(), risk_class_policies=policies).run(
        make_context(risk_class="C3", max_acceptable_loss=None), AS_OF).final
    assert loose.stress["equity_down_30_bond_down_5"] < -0.12                 # the cap really would matter
    assert tight.stress["equity_down_30_bond_down_5"] >= -0.12 - 1e-6
    assert tight.equity_weight < loose.equity_weight


def test_invalid_risk_class_is_rejected():
    with pytest.raises(ValueError):
        make_context(risk_class="C6")


def test_policy_table_must_be_complete_and_monotonic():
    policies = dict(AgentConfig().risk_class_policies)
    with pytest.raises(ValueError):
        AgentConfig(risk_class_policies={k: v for k, v in policies.items() if k != "C5"})
    policies["C2"] = RiskClassPolicy(risk_class="C2", max_equity=0.60, target_equity=0.5, max_vol=0.07, max_stress_loss=0.12)
    with pytest.raises(ValueError):
        AgentConfig(risk_class_policies=policies)               # C2 above C3 on equity cap
    with pytest.raises(ValueError):
        RiskClassPolicy(risk_class="C1", max_equity=0.10, target_equity=0.20, max_vol=0.04, max_stress_loss=0.08)


# ---- menu, behavioral constraints, infeasibility --------------------------
def test_menu_is_ordered_and_all_portfolios_satisfy_their_constraints():
    agent, context = make_agent(), make_context()
    menu = agent.build_candidate_menu(context, agent.prepare_market_model(context, AS_OF))
    assert [p.equity_weight for p in menu.portfolios] == sorted(p.equity_weight for p in menu.portfolios)
    assert all(p.status == "optimal" and p.violated_constraints == [] for p in menu.portfolios)
    vols = [p.risk.ex_ante_vol for p in menu.portfolios]
    assert vols[-1] > vols[len(vols) // 2] > 0                       # more equity, more risk (upper half)


def test_baseline_is_naive_and_reports_its_own_violations_honestly():
    agent, context = make_agent(), make_context(risk_class="C1")
    menu = agent.build_candidate_menu(context, agent.prepare_market_model(context, AS_OF))
    assert menu.baseline.stage == "baseline" and "constraints not enforced" in menu.baseline.message
    assert "suitability:min_cash" in menu.baseline.violated_constraints     # naive mix holds no cash


def test_portfolios_are_long_only_fully_invested_and_unleveraged():
    result = make_agent().run(make_context(), AS_OF)
    for p in [result.menu.baseline, *result.menu.portfolios, result.final]:
        assert sum(p.weights.values()) == pytest.approx(1.0, abs=1e-6)
        assert min(p.weights.values()) >= 0.0


def test_final_proposal_obeys_behavioral_limit_and_reports_it_as_binding():
    result = make_agent(StubBehavioralAgent(max_equity=0.25, confidence=0.9)).run(make_context(), AS_OF)
    assert result.final.equity_weight <= 0.25 + 1e-6
    assert "behavioral:max_equity" in result.final.binding_constraints


def test_low_confidence_behavioral_constraint_is_ignored_and_reported():
    result = make_agent(StubBehavioralAgent(max_equity=0.20, confidence=0.2)).run(make_context(), AS_OF)
    assert "behavioral:max_equity" in result.final.ignored_constraints
    assert result.final.equity_weight > 0.20                         # not enforced


def test_excluded_asset_gets_zero_weight():
    result = make_agent().run(make_context(excluded_assets=["EQ_EMERGING"]), AS_OF)
    assert result.final.weights["EQ_EMERGING"] == 0.0
    assert all(p.weights["EQ_EMERGING"] == 0.0 for p in result.menu.portfolios)


def test_liquidity_need_forces_cash_holding():
    result = make_agent().run(make_context(liquidity_need=0.20, min_cash_buffer=0.0), AS_OF)
    assert result.final.weights["CASH_MM"] >= 0.20 - 1e-6


def test_investor_capacity_loss_limit_can_be_tighter_than_the_class():
    result = make_agent().run(make_context(risk_class="C4", max_acceptable_loss=0.12), AS_OF)
    assert result.final.stress["equity_down_30_bond_down_5"] >= -0.12 - 1e-6
    assert result.final.equity_weight < 0.60                         # class target cannot be reached


def test_infeasible_problem_is_reported_not_raised():
    agent = make_agent()
    context = make_context(min_cash_buffer=0.9)       # one cash asset capped at 40%: impossible
    model = agent.prepare_market_model(context, AS_OF)
    menu = agent.build_candidate_menu(context, model)
    assert menu.portfolios == [] and menu.default_index is None
    final = agent.finalize_proposal(context, model, menu, [])
    assert final.status == "infeasible" and final.weights == {}


def test_soft_constraint_is_relaxed_and_status_becomes_fallback():
    agent, context = make_agent(), make_context()
    model = agent.prepare_market_model(context, AS_OF)
    menu = agent.build_candidate_menu(context, model)
    impossible_soft = Constraint(type="max_stress_loss", value=0.001, source="behavioral", hard=False,
                                 confidence=0.9, scenario="equity_down_30_bond_down_5")
    final = agent.finalize_proposal(context, model, menu, [impossible_soft])
    assert final.status == "fallback" and final.relaxed_constraints == [impossible_soft.label]
    assert final.violated_constraints == []                          # relaxed ones are reported as relaxed only


def test_hard_behavioral_constraint_that_cannot_be_met_gives_infeasible():
    agent, context = make_agent(), make_context()
    model = agent.prepare_market_model(context, AS_OF)
    menu = agent.build_candidate_menu(context, model)
    impossible_hard = Constraint(type="max_stress_loss", value=0.001, source="behavioral", hard=True,
                                 confidence=0.9, scenario="equity_down_30_bond_down_5")
    assert agent.finalize_proposal(context, model, menu, [impossible_hard]).status == "infeasible"


# ---- volatility regime and dynamic risk budget -----------------------------
def test_calm_market_leaves_the_risk_budget_untouched():
    final = crisis_agent(crisis=False).run(make_context(risk_class="C4"), AS_OF).final
    assert final.diagnostics["regime_label"] != "high" and final.diagnostics["risk_budget_scale"] == 1.0
    assert "risk_budget_vol" not in final.diagnostics


def test_volatility_spike_tightens_the_budget_and_lowers_equity():
    calm = crisis_agent(crisis=False).run(make_context(risk_class="C4"), AS_OF).final
    stressed = crisis_agent(crisis=True).run(make_context(risk_class="C4"), AS_OF).final
    assert stressed.diagnostics["regime_label"] == "high"
    assert stressed.diagnostics["risk_budget_scale"] < 1.0
    assert stressed.equity_weight < calm.equity_weight
    assert stressed.risk.ex_ante_vol <= stressed.diagnostics["risk_budget_vol"] + 1e-6 \
        or "system:max_vol" in stressed.relaxed_constraints            # honoured, or openly relaxed
    assert stressed.diagnostics["risk_budget_vol"] <= 0.13 + 1e-9      # never above the class cap (no leverage)


def test_dynamic_budget_can_be_switched_off():
    final = crisis_agent(crisis=True, dynamic_risk_budget=False).run(make_context(risk_class="C4"), AS_OF).final
    assert final.diagnostics["risk_budget_scale"] == 1.0


# ---- cost-aware trading ----------------------------------------------------
HOLDINGS = {"EQ_GLOBAL": 0.30, "BOND_GOVT": 0.50, "CASH_MM": 0.20}


def test_turnover_reported_only_when_holdings_given():
    assert make_agent().run(make_context(), AS_OF).final.implementation is None
    held = make_agent().run(make_context(current_holdings=HOLDINGS), AS_OF).final
    assert 0.0 < held.implementation.turnover <= 1.0 and held.implementation.estimated_cost > 0


def test_turnover_cap_limits_trading_and_is_reported():
    final = make_agent(NoBehavioralEvidence()).run(
        make_context(current_holdings=HOLDINGS, max_turnover=0.15), AS_OF).final
    assert final.implementation.turnover <= 0.15 + 1e-6
    assert "investor_preference:max_turnover" in final.binding_constraints or final.equity_weight < 0.40


def test_trading_cost_penalty_reduces_turnover():
    free = make_agent(NoBehavioralEvidence()).run(make_context(current_holdings=HOLDINGS), AS_OF).final
    sticky = make_agent(NoBehavioralEvidence(), turnover_penalty=500.0).run(
        make_context(current_holdings=HOLDINGS), AS_OF).final
    assert sticky.method.endswith("+cost_aware")
    assert sticky.implementation.turnover <= free.implementation.turnover + 1e-9
    assert sticky.implementation.estimated_cost <= free.implementation.estimated_cost + 1e-9
    assert sticky.status == "optimal" and sticky.violated_constraints == []     # still obeys every constraint


# ---- outputs and plumbing --------------------------------------------------
def test_risk_outputs_are_present_and_sane():
    final = make_agent(NoBehavioralEvidence()).run(make_context(), AS_OF).final
    assert final.risk.stressed_corr_vol >= final.risk.ex_ante_vol
    assert final.risk.n_tail_obs >= 1 and sum(final.risk.risk_contributions.values()) == pytest.approx(1.0)
    assert 0.0 < final.reverse_stress_equity_drop <= 1.0
    assert final.diagnostics["cov_estimator"] == "ledoit_wolf" and 0.0 <= final.diagnostics["shrinkage_intensity"] <= 1.0


def test_runs_are_reproducible_and_hash_changes_with_inputs():
    first = make_agent().run(make_context(), AS_OF).final
    second = make_agent().run(make_context(), AS_OF).final
    assert first.input_hash == second.input_hash and first.weights == second.weights
    assert make_agent(StubBehavioralAgent(max_equity=0.30)).run(make_context(), AS_OF).final.input_hash != first.input_hash
    assert make_agent().run(make_context(risk_class="C2"), AS_OF).final.input_hash != first.input_hash


def test_sensitivity_rows_cover_every_window_and_estimator():
    rows = make_agent().run(make_context(), AS_OF).final.sensitivity
    assert len(rows) == 9 and all(r.max_abs_weight_change >= 0 for r in rows)
    assert {r.estimator for r in rows} == {"sample", "ewma", "ledoit_wolf"}


def test_all_three_covariance_estimators_produce_valid_proposals():
    for estimator in ["sample", "ewma", "ledoit_wolf"]:
        final = make_agent(NoBehavioralEvidence(), cov_estimator=estimator).run(make_context(), AS_OF).final
        assert final.status == "optimal" and final.vol_estimator.startswith(estimator)


def test_mean_variance_needs_explicit_return_assumptions_and_runs_with_them():
    with pytest.raises(ValueError):
        make_agent(objective="mean_variance")
    assumed = {"EQ_GLOBAL": 0.06, "EQ_EMERGING": 0.07, "EQ_VALUE": 0.06,
               "BOND_GOVT": 0.03, "BOND_CORP": 0.04, "CASH_MM": 0.02}
    result = make_agent(objective="mean_variance", expected_returns=assumed).run(make_context(), AS_OF)
    assert result.final.status in {"optimal", "fallback"} and result.final.method.startswith("mean_variance")


def test_unknown_capacity_scenario_is_rejected():
    with pytest.raises(ValueError):
        make_agent(capacity_scenario="made_up")


def test_bad_data_from_agent_c_stops_the_run_with_a_report():
    class BrokenAgentC(SyntheticMarketAgent):
        def get_market_snapshot(self, asset_ids, as_of, lookback_days):
            snapshot = super().get_market_snapshot(asset_ids, as_of, lookback_days)
            snapshot.prices.iloc[10, 0] = np.nan
            return snapshot

    agent = QuantAgent(BrokenAgentC(), StubBehavioralAgent(), AgentConfig())
    with pytest.raises(DataValidationError) as error:
        agent.prepare_market_model(make_context(), AS_OF)
    assert not error.value.report.passed


def test_unknown_asset_in_context_is_rejected():
    with pytest.raises(ValueError):
        make_agent().prepare_market_model(make_context(excluded_assets=["NOT_IN_UNIVERSE"]), AS_OF)


def test_holdings_must_be_weights_that_sum_to_one():
    with pytest.raises(ValueError):
        make_context(current_holdings={"EQ_GLOBAL": 0.5})
    with pytest.raises(ValueError):
        make_context(current_holdings={"EQ_GLOBAL": 1.5, "BOND_GOVT": -0.5})


def test_stress_constraint_requires_scenario_name():
    with pytest.raises(ValueError):
        Constraint(type="max_stress_loss", value=0.1, source="behavioral")


def test_inflation_shock_moves_bonds_through_rates_only():
    from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS
    scenario = next(s for s in DEFAULT_STRESS_SCENARIOS if s.name == "inflation_shock")
    assert "bond" not in scenario.asset_class_shocks           # no flat bond shock on top of the rate shock
    # a 1.9-year bond loses about duration x 1.5%, not ~11%
    shock = qc.scenario_shock_vector(["bond"], [1.9], [5.0], scenario.asset_class_shocks, scenario.rate_shock_bp)[0]
    assert shock == pytest.approx(-1.9 * 0.015 + 0.5 * 5.0 * 0.015 ** 2)


def test_limit_that_stopped_a_higher_equity_level_is_reported():
    behavior = StubBehavioralAgent(max_equity=1.0, max_stress_loss=0.10, confidence=0.9)
    result = make_agent(behavior, run_sensitivity=False).run(make_context(risk_class="C4"), AS_OF)
    final = result.final
    assert final.equity_weight < 0.60 - 1e-6                    # lowered from the C4 target
    assert "behavioral:max_stress_loss[equity_down_30_bond_down_5]" in final.limiting_constraints


def test_no_limiting_constraints_when_the_target_level_is_reached():
    result = make_agent(NoBehavioralEvidence(), run_sensitivity=False).run(make_context(risk_class="C3"), AS_OF)
    assert result.final.equity_weight == pytest.approx(0.40, abs=1e-6)
    assert result.final.limiting_constraints == []


# ---- class caps: commodities, crypto, cash ---------------------------------------
def _expanded_agent(config: AgentConfig) -> QuantAgent:
    from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource

    interests = InvestorInterests(investor_id="t1", interest_areas=["stocks", "gold", "commodities", "crypto"])
    return QuantAgent(MarketIntelligenceAgent(interests, source=SyntheticPriceSource()), NoBehavioralEvidence(), config)


def _class_weight(proposal, agent, cls) -> float:
    classes = {a.asset_id: a.asset_class for a in agent.market_provider.get_market_snapshot(None, AS_OF, 300).assets}
    return sum(w for a, w in proposal.weights.items() if classes[a] == cls)


@pytest.mark.parametrize("risk_class", ["C3", "C4", "C5"])
def test_class_caps_hold_on_every_menu_portfolio_and_the_final(risk_class):
    config = AgentConfig(run_sensitivity=False)
    agent = _expanded_agent(config)
    policy = config.risk_class_policies[risk_class]
    result = agent.run(make_context(risk_class=risk_class, max_acceptable_loss=None), AS_OF)
    for p in [*result.menu.portfolios, result.final]:
        assert _class_weight(p, agent, "commodity") <= policy.max_commodity + 1e-6
        assert _class_weight(p, agent, "crypto") <= policy.max_crypto + 1e-6
        assert _class_weight(p, agent, "cash") <= max(policy.max_cash, 0.05) + 1e-6
        assert not p.violated_constraints


def test_a_tight_commodity_cap_binds_and_is_reported():
    policies = dict(DEFAULT_RISK_CLASS_POLICIES)
    for k, cap in (("C3", 0.0), ("C4", 0.01), ("C5", 0.01)):
        policies[k] = policies[k].model_copy(update={"max_commodity": cap})
    # On this synthetic path the all-bond end of the menu wants about 6% gold and commodities.
    capped = _expanded_agent(AgentConfig(run_sensitivity=False, risk_class_policies=policies))
    uncapped = _expanded_agent(AgentConfig(run_sensitivity=False))
    context = make_context(risk_class="C4", max_acceptable_loss=None)
    free = uncapped.run(context, AS_OF).menu.portfolios[0]
    tight = capped.run(context, AS_OF).menu.portfolios[0]
    assert _class_weight(free, uncapped, "commodity") > 0.02
    assert _class_weight(tight, capped, "commodity") == pytest.approx(0.01, abs=1e-6)
    assert "risk_class:max_class_weight[commodity]" in tight.binding_constraints


def test_class_caps_are_only_sent_for_classes_in_the_universe():
    agent = make_agent(run_sensitivity=False)                         # synthetic universe: no commodity/crypto
    final = agent.run(make_context(risk_class="C5"), AS_OF).final
    assert final.status != "infeasible"
    assert not any("[commodity]" in label or "[crypto]" in label for label in final.binding_constraints)


def test_max_class_weight_constraint_needs_an_asset_class():
    with pytest.raises(ValueError, match="asset_class"):
        Constraint(type="max_class_weight", value=0.1, source="risk_class")
    with pytest.raises(ValueError, match="asset_class"):
        Constraint(type="max_equity", value=0.1, source="risk_class", asset_class="equity")
    c = Constraint(type="max_class_weight", value=0.1, source="risk_class", asset_class="crypto")
    assert c.label == "risk_class:max_class_weight[crypto]"


def test_policy_table_rejects_rising_cash_or_falling_commodity_caps():
    policies = dict(DEFAULT_RISK_CLASS_POLICIES)
    policies["C5"] = policies["C5"].model_copy(update={"max_cash": 0.9})
    with pytest.raises(ValueError, match="max_cash"):
        AgentConfig(risk_class_policies=policies)
    policies = dict(DEFAULT_RISK_CLASS_POLICIES)
    policies["C5"] = policies["C5"].model_copy(update={"max_commodity": 0.0})
    with pytest.raises(ValueError, match="max_commodity"):
        AgentConfig(risk_class_policies=policies)


def test_instrument_cap_from_agent_c_binds_and_is_reported():
    from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource

    interests = InvestorInterests(investor_id="t1", interest_areas=["technology", "healthcare"],
                                  instrument_style="individual")
    agent = QuantAgent(MarketIntelligenceAgent(interests, source=SyntheticPriceSource()), NoBehavioralEvidence(),
                       AgentConfig(run_sensitivity=False))
    result = agent.run(make_context(risk_class="C5", max_acceptable_loss=None), AS_OF)
    stocks = {a.asset_id for a in agent.market_provider.get_market_snapshot(None, AS_OF, 300).assets if a.max_weight}
    assert stocks
    for p in [*result.menu.portfolios, result.final]:
        assert all(p.weights[s] <= 0.10 + 1e-6 for s in stocks)
    capped = [label for p in result.menu.portfolios for label in p.binding_constraints
              if label.startswith("system:instrument_cap[")]
    assert all(label[len("system:instrument_cap["):-1] in stocks for label in capped)


def test_slsqp_singular_first_step_is_retried_without_equality_rows(monkeypatch):
    """SLSQP sometimes stops at step 1 ("Singular matrix C in LSQ subproblem"); the solver must retry."""
    import numpy as np
    import egoquant.agents.quant.core as qc

    real = qc.minimize
    calls = []

    def flaky(fun, x0, **kwargs):
        calls.append([c["type"] for c in kwargs["constraints"]])
        result = real(fun, x0, **kwargs)
        if len(calls) == 1:                       # first attempt: pretend SLSQP failed like in the wild
            result.status, result.x = 6, x0 * 0.0
        return result

    monkeypatch.setattr(qc, "minimize", flaky)
    cov = np.array([[0.04, 0.01], [0.01, 0.02]])
    out = qc.solve_portfolio(cov, ["A", "B"], np.array([1.0, 1.0]), ["cap", "cap"], [], [],
                              np.array([1.0, 0.0]))
    assert out.status == "optimal" and abs(out.weights.sum() - 1.0) < 1e-6
    assert "eq" in calls[0] and "eq" not in calls[1]
