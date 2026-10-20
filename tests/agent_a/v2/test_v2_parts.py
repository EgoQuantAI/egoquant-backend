"""Portfolio method v2: the selection rule, the solve limits, the universe screening and the diagnostics, piece by piece."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.quant.v2 import diag, selection, universe
from egoquant.agents.quant.v2 import solve as v2solve
from egoquant.models import AgentConfig, AssetInfo, Constraint


def outcome(index, chance, drawdown):
    return SimpleNamespace(menu_index=index, prob_goal=chance, drawdown_p90=drawdown, worst5_shortfall=0.0)


# ---- selection (docs/PORTFOLIO_V2.md 2.3) ----------------------------------------------------------------
@pytest.mark.parametrize("best,expected", [(0.0, 0.01), (0.05, 0.01), (0.20, 0.02), (0.30, 0.03), (0.70, 0.03), (1.0, 0.03)])
def test_tolerance_is_max_1_point_min_3_points_10_percent(best, expected):
    assert selection.tolerance(best) == pytest.approx(expected)


def test_lowest_drawdown_inside_the_tolerance_wins_not_the_best_chance():
    outs = [outcome(0, 0.70, 0.30), outcome(1, 0.69, 0.22), outcome(2, 0.675, 0.10), outcome(3, 0.60, 0.05)]
    ranked, reason, info = selection.select_goal(outs, {i: 0.1 for i in range(4)}, {i: 8.0 for i in range(4)})
    assert [o.menu_index for o in ranked][:3] == [2, 1, 0]          # 3 is outside the 3-point band although its drawdown is lowest
    assert info["v2_n_contenders"] == 3 and info["v2_tolerance_points"] == pytest.approx(3.0)
    assert "worst 10%" in reason


def test_ties_go_to_more_effective_holdings_then_lower_volatility_then_index():
    outs = [outcome(0, 0.5, 0.2), outcome(1, 0.5, 0.2), outcome(2, 0.5, 0.2)]
    ranked, _, _ = selection.select_goal(outs, {0: 0.1, 1: 0.2, 2: 0.1}, {0: 5.0, 1: 9.0, 2: 9.0})
    assert [o.menu_index for o in ranked] == [2, 1, 0]


def test_the_diversification_floor_and_the_holdings_cap_filter_the_contenders_when_possible():
    outs = [outcome(0, 0.6, 0.05), outcome(1, 0.6, 0.20), outcome(2, 0.6, 0.30)]
    ranked, _, info = selection.select_goal(outs, {}, {0: 3.0, 1: 7.0, 2: 9.0}, floor=6.0,
                                            n_holdings={0: 5, 1: 12, 2: 100})
    assert ranked[0].menu_index == 1 and info["v2_floor_met"] == 1.0
    ranked, _, info = selection.select_goal(outs, {}, {0: 3.0, 1: 4.0, 2: 5.0}, floor=6.0)
    assert ranked[0].menu_index == 0 and info["v2_floor_met"] == 0.0         # nobody meets the floor: the plain rule


# ---- solve limits ----------------------------------------------------------------------------------------
class FakeAgent:
    def __init__(self, cap):
        self.cap = cap

    def _policy(self, context):
        return SimpleNamespace(v2_max_fund_weight=self.cap)


def test_the_v2_per_fund_cap_replaces_a_looser_investor_cap_and_never_a_tighter_one():
    ctx = SimpleNamespace()
    loose = [Constraint(type="max_asset_weight", value=0.40, source="investor_preference")]
    tight = [Constraint(type="max_asset_weight", value=0.20, source="investor_preference")]
    assert v2solve.cap_constraints(FakeAgent(0.30), ctx, loose)[0].value == 0.30
    assert v2solve.cap_constraints(FakeAgent(0.30), ctx, tight)[0].value == 0.20
    assert v2solve.cap_constraints(FakeAgent(None), ctx, loose)[0].value == 0.40


def test_group_rows_cap_a_group_at_25_percent_and_half_of_the_stock_sleeve():
    ids = ["A", "B", "C", "D"]
    state = universe.V2State(n_universe=4, kept=ids, cluster_of={}, groups=[["A", "B"]])
    model = SimpleNamespace(asset_ids=ids, asset_classes=["equity"] * 4)
    rows = v2solve.group_rows(model, state, np.array([True, True, True, False]))
    assert [r[0].split("<=")[1] for r in rows] == ["portfolio", "stock sleeve"]
    portfolio, sleeve = rows
    assert portfolio[2] == 0.25 and list(portfolio[1]) == [1, 1, 0, 0]
    w_ok = np.array([0.20, 0.10, 0.30, 0.40])       # group 30% of the portfolio is too much, but half the stock sleeve (60%) is fine
    assert float(sleeve[1] @ w_ok) <= 0.0 + 1e-12
    assert float(portfolio[1] @ w_ok) > portfolio[2]


# ---- universe screening ----------------------------------------------------------------------------------
def synthetic_universe(n_per_group=5, groups=6, days=300, seed=3):
    rng = np.random.default_rng(seed)
    cols, data, infos = [], [], []
    base = rng.normal(0, 0.01, (days, groups))
    for g in range(groups):
        for k in range(n_per_group):
            name = f"G{g}F{k}"
            cols.append(name)
            data.append(base[:, g] + rng.normal(0, 0.0005, days))
            infos.append(AssetInfo(asset_id=name, asset_class="equity", expense_ratio=0.001 * (k + 1), liquidity_tier=1))
    return pd.DataFrame(np.array(data).T, columns=cols), infos


def test_screening_keeps_one_cheapest_representative_per_cluster_and_the_forced_ones():
    returns, infos = synthetic_universe()
    kept, cluster_of, short, _ = universe.screen_assets(returns, infos, forced={"G2F3"}, max_representatives=6)
    assert len(kept) == 6 + 0 or len(kept) == 7
    assert "G2F3" in kept
    cheapest = {f"G{g}F0" for g in range(6)}
    assert cheapest <= set(kept) | {"G2F0"}                         # the cluster of the forced fund may keep the forced one instead
    assert short == [] and set(cluster_of) == set(returns.columns)


def test_screening_is_deterministic_and_keeps_every_asset_class():
    returns, infos = synthetic_universe()
    infos = [i.model_copy(update={"asset_class": "bond" if i.asset_id.startswith("G0") else "equity"}) for i in infos]
    a = universe.screen_assets(returns, infos, set(), max_representatives=4)
    b = universe.screen_assets(returns, infos, set(), max_representatives=4)
    assert a[0] == b[0]
    classes = {i.asset_class for i in infos if i.asset_id in set(a[0])}
    assert classes == {"bond", "equity"}


def test_near_duplicate_groups_collect_funds_with_the_same_returns_only():
    returns, _ = synthetic_universe(n_per_group=3, groups=3)
    classes = {c: "equity" for c in returns.columns}
    groups = universe.near_duplicate_groups(returns, classes)
    assert sorted(len(g) for g in groups) == [3, 3, 3]
    assert all(len({m[:2] for m in g}) == 1 for g in groups)


# ---- diagnostics -----------------------------------------------------------------------------------------
def test_describe_reports_effective_n_largest_and_risk_contributions_as_flat_values():
    cov = np.diag([0.04, 0.01, 0.01])
    model = SimpleNamespace(asset_ids=["A", "B", "C"], cov_annual=cov, v2=universe.V2State(n_universe=9, kept=["A", "B", "C"], cluster_of={}))
    out = diag.describe(model, np.array([0.5, 0.25, 0.25]), ["v2:group[0]<=portfolio"])
    assert out["v2_effective_n"] == pytest.approx(1 / (0.25 + 0.0625 * 2))
    assert out["v2_largest"] == 0.5 and out["v2_n_universe"] == 9.0 and out["v2_group_cap_binding"] == 1.0
    assert all(isinstance(v, (float, str)) for v in out.values())
