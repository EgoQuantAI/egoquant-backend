"""Rate anchor of the goal simulation: bond / cash averages set to the short rate at the decision date.

Found by the calibration backtest (eval/results/zc_z2_*). Run with:  pytest -v tests/agent_a/test_goal_rate_anchor.py
"""

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.quant import goal as gq
from tests.helpers import make_answers
from tests.system.test_goal_based import goal, run


def _daily(days=400, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=days)
    return pd.DataFrame({"BOND": rng.normal(0.0003, 0.003, days), "CASH": rng.normal(0.0002, 0.0001, days),
                         "STOCK": rng.normal(0.0005, 0.01, days)}, index=idx)


CLASSES = {"BOND": "bond", "CASH": "cash", "STOCK": "equity"}


def test_short_rate_annualizes_the_last_window():
    prices = pd.Series(1.0001 ** np.arange(200))                      # +0.01% every trading day
    assert gq.short_rate(prices, window=63) == pytest.approx(1.0001 ** 252 - 1)
    assert gq.short_rate(prices.iloc[:63], window=63) is None           # too little history


def test_anchor_sets_bond_and_cash_means_and_keeps_the_daily_moves():
    daily = _daily()
    out, anchor = gq.anchor_rate_means(daily, CLASSES, rate=0.02, term_premium=0.015)
    daily_target = lambda annual: (1 + annual) ** (1 / 252) - 1         # noqa: E731
    assert out["CASH"].mean() == pytest.approx(daily_target(0.02))
    assert out["BOND"].mean() == pytest.approx(daily_target(0.035))
    pd.testing.assert_series_equal(out["STOCK"], daily["STOCK"])        # other classes untouched
    for col in ("BOND", "CASH"):                                        # only the level moves, not the shape
        assert (out[col] - daily[col]).std() == pytest.approx(0.0, abs=1e-15)
        assert out[col].std() == pytest.approx(daily[col].std())
    assert anchor.short_rate == pytest.approx(0.02) and anchor.term_premium == 0.015
    assert {n.ticker for n in anchor.assets} == {"BOND", "CASH"}
    note = next(n for n in anchor.assets if n.ticker == "BOND")
    assert note.adjusted_return == pytest.approx(round(daily_target(0.035) * 252, 4))
    assert note.sample_return == pytest.approx(round(daily["BOND"].mean() * 252, 4))


def test_a_negative_short_rate_is_allowed():
    out, anchor = gq.anchor_rate_means(_daily(), CLASSES, rate=-0.001, term_premium=0.015)
    assert out["CASH"].mean() < 0 < out["BOND"].mean()


@pytest.fixture(scope="module")
def goal_run():
    answers = make_answers(gl_score=25)                                 # C3: bonds and cash are held
    return run(answers, goal=goal(answers, multiple=1.4, monthly=100.0, years=10.0))


def test_goal_path_records_and_applies_the_anchor(goal_run):
    ra = goal_run.goal_analysis.rate_anchor
    assert ra is not None and ra.proxy == gq.SHORT_RATE_PROXY and ra.term_premium == gq.BOND_TERM_PREMIUM
    classes = {i.ticker: i.asset_class for i in goal_run.agent_c.selection.instruments}
    assert ra.assets and all(classes[n.ticker] in gq.RATE_ANCHOR_CLASSES for n in ra.assets)
    for n in ra.assets:
        expected = ra.short_rate + (ra.term_premium if n.asset_class == "bond" else 0.0)
        assert n.adjusted_return == pytest.approx(((1 + expected) ** (1 / 252) - 1) * 252, abs=1e-4)
    assert "Rate anchor:" in goal_run.goal_analysis.history_note


def test_switching_it_off_restores_the_sample_averages(monkeypatch):
    answers = make_answers(gl_score=25)
    spec = goal(answers, multiple=1.4, monthly=100.0, years=10.0)
    monkeypatch.setattr(gq, "RATE_ANCHOR", False)
    off = run(answers, goal=spec)
    assert off.goal_analysis.rate_anchor is None and "Rate anchor:" not in off.goal_analysis.history_note


def test_anchored_goal_path_is_deterministic(goal_run):
    answers = make_answers(gl_score=25)
    again = run(answers, goal=goal(answers, multiple=1.4, monthly=100.0, years=10.0))
    assert again.goal_analysis.model_dump() == goal_run.goal_analysis.model_dump()


# ---- What-If replays the report's anchor exactly, and never adds one to a report built without it ----
@pytest.fixture(scope="module")
def what_if_base():
    import os
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from egoquant.agents import what_if as wi
    from tests.system.test_what_if import ENV, portfolio_body
    with patch.dict(os.environ, ENV):
        from main import app
        out = TestClient(app).post("/portfolio", json=portfolio_body()).json()
        return out, wi.open_what_if_context(out["what_if_context"])


def test_the_what_if_context_carries_the_reports_rate(what_if_base):
    out, base = what_if_base
    anchor = base["rate_anchor"]
    assert anchor["term_premium"] == gq.BOND_TERM_PREMIUM and isinstance(anchor["short_rate"], float)


def test_a_report_without_the_anchor_is_replayed_without_it(what_if_base):
    import os
    from unittest.mock import patch

    from egoquant.agents import what_if as wi
    from egoquant.models import AgentConfig
    from tests.system.test_what_if import ENV
    _, base = what_if_base
    cfg, cols = AgentConfig(), sorted(set(base["goal_assets"]) | set(base["weights"]))
    bonds = [t for t in cols if base["classes"].get(t) in gq.RATE_ANCHOR_CLASSES]
    assert bonds
    with patch.dict(os.environ, ENV):
        anchored, _ = wi.goal_history(base, cols, cfg)
        old = {k: v for k, v in base.items() if k != "rate_anchor"}          # a report from before the anchor
        plain, _ = wi.goal_history(old, cols, cfg)
    for t in bonds:
        extra = gq.BOND_TERM_PREMIUM if base["classes"][t] == "bond" else 0.0
        assert anchored[t].mean() == pytest.approx((1 + base["rate_anchor"]["short_rate"] + extra) ** (1 / 252) - 1)
        assert plain[t].mean() != pytest.approx(anchored[t].mean())
    others = [t for t in cols if t not in bonds and t in plain.columns]
    pd.testing.assert_frame_equal(plain[others], anchored[others])
