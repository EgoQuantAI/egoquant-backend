"""Portfolio method v2 end to end on the synthetic market: the limits still bind, the portfolio is more diversified, the result is
reproducible, and with the flag off nothing changes. Selection, solve and universe pieces are tested in test_v2_parts.py."""

from datetime import date

import numpy as np
import pytest

from egoquant.agents.behavioral import NoBehavioralEvidence
from egoquant.agents.quant import QuantAgent
from egoquant.agents.quant import v2
from egoquant.demo.stubs import SyntheticMarketAgent
from egoquant.models import AgentConfig, InvestorContext

AS_OF = date(2026, 9, 30)
FLAG = "EGOQUANT_PORTFOLIO_METHOD"


def context(risk_class="C4", **kw) -> InvestorContext:
    base = dict(investor_id="t1", risk_class=risk_class, goal_type="growth", horizon_years=10, min_cash_buffer=0.05,
                max_acceptable_loss=0.30, suitability_max_equity=0.90, max_asset_weight=0.40)
    base.update(kw)
    return InvestorContext(**base)


def final(monkeypatch, method: str, risk_class="C4", **kw):
    if method:
        monkeypatch.setenv(FLAG, method)
    else:
        monkeypatch.delenv(FLAG, raising=False)
    agent = QuantAgent(SyntheticMarketAgent(seed=7), NoBehavioralEvidence(), AgentConfig(run_sensitivity=False))
    return agent.run(context(risk_class, **kw), AS_OF).final


def effective_n(weights: dict[str, float]) -> float:
    w = np.array([v for v in weights.values() if v > 1e-9])
    return 1.0 / float(np.sum(w * w))


def test_the_flag_is_v1_unless_it_is_exactly_v2(monkeypatch):
    for value, expected in (("", "v1"), ("v1", "v1"), ("V2 ", "v2"), ("v2", "v2"), ("2", "v1"), ("yes", "v1")):
        monkeypatch.setenv(FLAG, value)
        assert v2.portfolio_method() == expected
    monkeypatch.delenv(FLAG, raising=False)
    assert v2.portfolio_method() == "v1"


@pytest.mark.parametrize("risk_class", ["C3", "C4", "C5"])
def test_v2_keeps_every_hard_limit_and_caps_one_fund_at_thirty_percent(monkeypatch, risk_class):
    policy = AgentConfig().risk_class_policies[risk_class]
    p = final(monkeypatch, "v2", risk_class)
    assert p.status in {"optimal", "fallback"} and not p.violated_constraints
    assert p.equity_weight <= policy.max_equity + 1e-6
    assert p.risk.ex_ante_vol <= policy.max_vol + 1e-6
    assert max(p.weights.values()) <= 0.30 + 1e-6
    assert abs(sum(p.weights.values()) - 1.0) < 1e-6
    assert p.method.startswith("v2:") and p.engine_version.endswith("+pv2")
    d = p.diagnostics
    assert d["v2_effective_n"] > 1 and d["v2_largest"] <= 0.30 + 1e-6 and "v2_risk_contribution" in d


@pytest.mark.parametrize("risk_class", ["C3", "C4", "C5"])
def test_v2_is_more_diversified_than_the_default_minimum_variance_portfolio(monkeypatch, risk_class):
    old = final(monkeypatch, "", risk_class)
    new = final(monkeypatch, "v2", risk_class)
    assert effective_n(new.weights) >= effective_n(old.weights) - 1e-9
    assert max(new.weights.values()) <= max(old.weights.values()) + 1e-9


def test_same_input_twice_gives_the_same_portfolio_and_other_input_gives_another(monkeypatch):
    a = final(monkeypatch, "v2", "C4")
    b = final(monkeypatch, "v2", "C4")
    assert a.weights == b.weights and a.input_hash == b.input_hash
    c = final(monkeypatch, "v2", "C4", horizon_years=15, interest_assets=["VGT"] if False else [])
    assert c.input_hash != a.input_hash


def test_v1_is_untouched_when_the_flag_is_off(monkeypatch):
    p = final(monkeypatch, "", "C4")
    assert not p.method.startswith("v2") and not p.engine_version.endswith("+pv2")
    assert not any(k.startswith("v2_") for k in p.diagnostics)


def test_c2_without_stocks_still_works_under_v2(monkeypatch):
    p = final(monkeypatch, "v2", "C2")
    assert p.status in {"optimal", "fallback"} and p.equity_weight <= 1e-6 and abs(sum(p.weights.values()) - 1.0) < 1e-6
