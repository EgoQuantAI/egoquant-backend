"""What-If consistency with the regime sampler: same_portfolio's baseline equals the report's chance EXACTLY, also
after the flag is switched off, because the what-if replays the report's own sampler (egoquant/agents/what_if.py).

The /portfolio route runs on SYNTHETIC prices in tests (no network). The regime sampler refuses synthetic data, so
these tests let it run by passing data_source "historical" to the decision (the labels only use the dates).
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from egoquant.agents import what_if as wi
from egoquant.agents.quant import goal as gq
from egoquant.models import AgentConfig
from egoquant.regime import hook
from tests.regime.conftest import fixed_recent_returns
from tests.system.test_what_if import ENV, portfolio_body


def _allow_synthetic(monkeypatch):
    real = hook._decide
    monkeypatch.setattr(hook, "_decide", lambda d, b, a, ds, f, m: real(d, b, a, "historical", f, m))
    fixed_recent_returns(monkeypatch)


def _build(flag: str) -> dict:
    import egoquant_routes as routes
    routes._portfolio_timestamps.clear()
    with patch.dict(os.environ, {**ENV, hook.FLAG: flag}):
        from main import app
        return TestClient(app).post("/portfolio", json=portfolio_body()).json()


@pytest.fixture(scope="module")
def reports():
    mp = pytest.MonkeyPatch()
    _allow_synthetic(mp)
    try:
        yield {"on": _build("1"), "off": _build("0")}
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    _allow_synthetic(monkeypatch)
    with patch.dict(os.environ, ENV):
        wi._rate_hits.clear()
        yield
        wi._rate_hits.clear()


def exact_baseline(token: str) -> float:
    """The same_portfolio baseline computed exactly as WhatIf._run does it, at full precision."""
    base = wi.open_what_if_context(token)
    cfg = AgentConfig()
    b_spec, _ = wi._specs(base, wi.WhatIfScenario(extra_monthly_contribution=1))
    daily, _ = wi.goal_history(base, sorted(set(base["goal_assets"]) | set(base["weights"])), cfg)
    regime = hook.decide(daily.index, cfg.goal_block_days, date.fromisoformat(base["as_of"]), base.get("data_source"),
                         forced=base.get("sampler") or {"sampler": hook.SAMPLER_BOOTSTRAP})
    blocks = gq.block_returns(daily, cfg.goal_block_days)
    d, _ = wi._draws(len(blocks), wi.WHAT_IF_PATHS, b_spec.months, b_spec.months, cfg.goal_seed, regime)
    w = gq.simulate_wealth(blocks, np.array([base["weights"].get(a, 0.0) for a in daily.columns]),
                           np.array([base["fees"].get(a, 0.0) for a in daily.columns]), b_spec, d)
    return float(np.mean(w[:, -1] >= b_spec.goal_amount))


def test_the_report_used_the_regime_sampler_and_the_context_carries_it(reports):
    out = reports["on"]
    assert out["goal"]["sampler"]["sampler"] == "regime_hmm_v2"
    assert out["report"]["engine_version"].startswith("0.4.0+regime_hmm_v2.")
    base = wi.open_what_if_context(out["what_if_context"])
    assert base["sampler"] == out["goal"]["sampler"]
    off = wi.open_what_if_context(reports["off"]["what_if_context"])
    assert "sampler" not in off and "sampler" not in reports["off"]["goal"]


def test_same_portfolio_baseline_equals_the_report_exactly(reports, monkeypatch):
    out = reports["on"]
    assert exact_baseline(out["what_if_context"]) == out["goal"]["chosen"]["prob_goal"]
    monkeypatch.setenv(hook.FLAG, "0")                            # the flag changes after the report: still equal
    assert exact_baseline(out["what_if_context"]) == out["goal"]["chosen"]["prob_goal"]


def test_run_what_if_reports_the_same_baseline_and_names_the_sampler(reports):
    from egoquant.report import _in_100
    out = reports["on"]
    res = wi.run_what_if(out["what_if_context"], {"extra_monthly_contribution": 200}, timeout_s=120)
    assert res.status == "ok" and res.baseline_goal_frequency == _in_100(out["goal"]["chosen"]["prob_goal"])
    assert res.scenario_goal_frequency >= res.baseline_goal_frequency
    assert res.sampler["sampler"] == "regime_hmm_v2" and res.sampler["replayed_from_report"] is True
    assert res.engine_version.startswith("what-if-0.2.0+regime_hmm_v2.")
    longer = wi.run_what_if(out["what_if_context"], {"horizon_years": 25}, timeout_s=120)
    assert longer.status == "ok" and longer.baseline_goal_frequency == res.baseline_goal_frequency


@pytest.mark.parametrize("flag_now", ["1", None])          # None: unset, i.e. the default (on)
def test_a_report_built_with_the_flag_off_stays_on_the_block_bootstrap(reports, monkeypatch, flag_now):
    """Reports stored before the flag was on by default have no sampler record: their What-If stays on the block
    bootstrap, so the baseline still equals the stored chance."""
    out = reports["off"]
    if flag_now is None:
        monkeypatch.delenv(hook.FLAG, raising=False)
    else:
        monkeypatch.setenv(hook.FLAG, flag_now)
    assert exact_baseline(out["what_if_context"]) == out["goal"]["chosen"]["prob_goal"]
    res = wi.run_what_if(out["what_if_context"], {"extra_monthly_contribution": 200}, timeout_s=120)
    assert res.status == "ok" and res.sampler is None and res.engine_version == "what-if-0.1.0"
    assert "sampler" not in res.model_dump()


def test_a_changed_model_file_is_recorded_not_hidden(reports, monkeypatch, tmp_path):
    import json
    from egoquant.regime.model import MODEL_PATH, content_hash
    body = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    body["fit"] = {**body["fit"], "note": "refitted"}
    body["sha256"] = content_hash(body)
    p = tmp_path / "other.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    from egoquant.regime import model as regime_model
    real_load = regime_model.load_regime_model
    monkeypatch.setattr(hook, "load_regime_model", lambda path=None: real_load(p))   # a different deployed model
    res = wi.run_what_if(reports["on"]["what_if_context"], {"extra_monthly_contribution": 200}, timeout_s=120)
    assert res.status == "ok" and res.sampler["sampler"] == "block_bootstrap"
    assert res.sampler["fallback_reason"] == "model_changed"
