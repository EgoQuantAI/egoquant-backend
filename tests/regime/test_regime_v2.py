"""Regime model version 2 (eval/regime/PROTOCOL_v2.md): 2-4 states chosen by BIC, optional bond features (SPY + AGG).

The runtime loads both versions through the same code; version 1 must behave exactly as before. Uses a committed
walk-forward v2 artifact (eval/results/regime_walkforward/) so no test depends on which v2 is deployed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from egoquant.regime import hook
from egoquant.regime import model as rm
from egoquant.regime.model import RegimeModelError, content_hash, current_regime_probs, features, parse_regime_model
from egoquant.regime.sampler import RegimeSampler, chain_stats
from tests.regime.conftest import AS_OF, calm_recent_returns, calm_two_fund_returns

ROOT = Path(__file__).resolve().parents[2]
JOINT = ROOT / "eval" / "results" / "regime_walkforward" / "regime_hmm_v2_joint_asof_2021-06-30.json"
SPY_ONLY = ROOT / "eval" / "results" / "regime_walkforward" / "regime_hmm_v2_spy_asof_2021-06-30.json"


@pytest.fixture(scope="module")
def joint_body() -> dict:
    return json.loads(JOINT.read_text(encoding="utf-8"))


def rehash(body: dict) -> dict:
    return {**body, "sha256": content_hash(body)}


# ==================================================================================================================
# Loading
# ==================================================================================================================
def test_a_joint_v2_artifact_loads_with_its_own_states_and_funds(joint_body):
    m = rm.load_regime_model(JOINT)
    assert m.version == "regime_hmm_v2" and m.tickers == ("SPY", "AGG")
    assert m.n_states == joint_body["n_components"] == len(m.state_names) and m.state_names[-1] == "crisis"
    assert m.means.shape == (m.n_states, 4) and m.covariances.shape == (m.n_states, 4, 4)
    sel = joint_body["selection"]
    assert sel["states_tried"] == [2, 3, 4] and sel["restarts_per_k"] == 10
    best = min(sel["candidates"], key=lambda c: c["bic"])
    assert best["n_components"] == m.n_states                       # K chosen by BIC on the training data


def test_the_spy_only_v2_artifact_reads_one_fund():
    m = rm.load_regime_model(SPY_ONLY)
    assert m.tickers == ("SPY",) and m.means.shape == (m.n_states, 2)


@pytest.mark.parametrize("change", [
    lambda b: b.update(n_components=5),
    lambda b: b["features"].update(tickers=["AGG", "SPY"]),
    lambda b: b["features"].update(tickers=["SPY", "SPY"]),
    lambda b: b.update(state_names=["calm", "normal", "crisis", "stressed"][: len(b["state_names"])][::-1]),
    lambda b: b.update(model_version="regime_hmm_v9"),
])
def test_invalid_v2_artifacts_are_refused(joint_body, change):
    body = json.loads(json.dumps(joint_body))
    change(body)
    with pytest.raises(RegimeModelError) as err:
        parse_regime_model(rehash(body))
    assert err.value.reason == "artifact_invalid"


def test_features_of_several_funds_are_each_fund_s_return_and_volatility():
    m = rm.load_regime_model(JOINT)
    r = calm_two_fund_returns()[0]
    x = features(r, m)
    w = m.vol_window
    assert x.shape == (len(r) - w + 1, 4)
    raw_agg_vol = x[:, 3] * m.feature_std[3] + m.feature_mean[3]
    assert np.allclose(raw_agg_vol[0], r[:w, 1].std())
    probs, source = current_regime_probs(r, m)
    assert source == "filtered" and probs.shape == (m.n_states,) and abs(probs.sum() - 1) < 1e-12


def test_recent_model_returns_aligns_the_funds_on_common_days(tmp_path, monkeypatch):
    from egoquant import settings
    monkeypatch.setattr(settings, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "price_snapshot_enabled", lambda: False)
    (tmp_path / "tiingo").mkdir()
    spy = {"2026-10-12": 100.0, "2026-10-13": 101.0, "2026-10-14": 102.0, "2026-10-15": 101.0}
    agg = {"2026-10-12": 50.0, "2026-10-14": 50.5, "2026-10-15": 50.25}            # no 10-13
    for t, p in (("SPY", spy), ("AGG", agg)):
        (tmp_path / "tiingo" / f"{t}.json").write_text(json.dumps({"prices": p}), encoding="utf-8")
    returns, last, gap = rm.recent_model_returns(date(2026, 10, 16), rm.load_regime_model(JOINT))
    assert returns.shape == (2, 2) and str(last) == "2026-10-15" and gap == 1
    assert np.allclose(returns[0], [np.log(102 / 100), np.log(50.5 / 50)])


def test_the_snapshot_is_not_read_when_the_cache_reaches_today(tmp_path, monkeypatch):
    from egoquant import settings
    monkeypatch.setattr(settings, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "price_snapshot_enabled", lambda: True)
    monkeypatch.setattr(rm, "_snapshot_prices", lambda t: pytest.fail("the snapshot was loaded"))
    (tmp_path / "tiingo").mkdir()
    for t in ("SPY", "AGG"):
        prices = {"2026-10-14": 100.0, "2026-10-15": 101.0, "2026-10-16": 100.5}
        (tmp_path / "tiingo" / f"{t}.json").write_text(json.dumps({"prices": prices}), encoding="utf-8")
    returns, last, gap = rm.recent_model_returns(date(2026, 10, 16), rm.load_regime_model(JOINT))
    assert returns.shape == (2, 2) and gap == 0


# ==================================================================================================================
# The sampler with K states
# ==================================================================================================================
def test_chains_with_three_states_are_the_version_1_inverse_cdf():
    """The generalised chain step gives exactly the draws of the version-1 formula (u >= lo) + (u >= hi)."""
    p21 = np.array([[0.8, 0.15, 0.05], [0.3, 0.5, 0.2], [0.1, 0.4, 0.5]])
    start = np.array([0.6, 0.3, 0.1])
    s = RegimeSampler([np.arange(10), np.arange(10, 20), np.arange(20, 30)], start, p21)
    got = s.chains(500, 24, np.random.default_rng(7))
    rng = np.random.default_rng(7)
    cut, row = np.cumsum(start)[:-1], np.cumsum(p21, axis=1)[:, :-1]
    exp = np.empty((24, 500), dtype=np.int8)
    prev = None
    for m in range(24):
        u = rng.random(500)
        lo, hi = (cut if prev is None else (row[prev, 0], row[prev, 1]))
        exp[m] = (u >= lo).astype(np.int8) + (u >= hi).astype(np.int8)
        prev = exp[m]
    assert np.array_equal(got, exp.T)


@pytest.mark.parametrize("k", [2, 4])
def test_two_and_four_state_chains_follow_the_transition_matrix(k):
    rng = np.random.default_rng(1)
    p21 = rng.dirichlet(np.ones(k) * 3, size=k)
    pools = [np.arange(i * 10, i * 10 + 10) for i in range(k)]
    s = RegimeSampler(pools, np.full(k, 1 / k), p21)
    chains = s.chains(40_000, 2, np.random.default_rng(2))
    assert set(np.unique(chains)) <= set(range(k))
    for i in range(k):
        nxt = chains[chains[:, 0] == i, 1]
        assert np.allclose(np.bincount(nxt, minlength=k) / len(nxt), p21[i], atol=0.02)
    d = s.draws(1000, 12, 3)
    assert d.shape == (1000, 12) and d.max() < k * 10
    assert len(s.stats["month_shares"]) == k


def test_long_crises_count_the_most_volatile_state():
    chains = np.array([[3, 3, 3, 0], [2, 2, 2, 2]], dtype=np.int8)
    assert chain_stats(chains, 4)["long_crisis_share"] == 0.5
    assert chain_stats(np.array([[1, 1, 1]], dtype=np.int8), 2)["long_crisis_share"] == 1.0


# ==================================================================================================================
# decide(), replay and surfacing
# ==================================================================================================================
def test_decide_with_a_joint_v2_model(daily, monkeypatch):
    monkeypatch.setenv(hook.FLAG, "1")
    monkeypatch.setattr(hook, "recent_model_returns", lambda as_of, model: calm_two_fund_returns())
    dec = hook.decide(daily.index, 21, AS_OF, "historical", model_path=JOINT)
    rec = dec.record()
    assert dec.active and rec["sampler"] == "regime_hmm_v2" and hook.is_regime(rec)
    assert rec["model_funds"] == ["SPY", "AGG"] and len(rec["pool_blocks"]) == len(rec["state_names"])
    assert dec.draws(rec["n_blocks"], 500, 24, 1).shape == (500, 24)
    assert "regime_hmm_v2" in hook.hash_part(rec)
    assert hook.engine_version(rec, "0.3.0").startswith("0.4.0+regime_hmm_v2.")


def test_a_report_replays_with_the_model_version_it_used(daily, monkeypatch):
    """A v1 report still replays with v1 after v2 becomes the default, and a v2 report with v2."""
    monkeypatch.setenv(hook.FLAG, "1")
    monkeypatch.setattr(hook, "recent_market_returns", lambda as_of: calm_recent_returns())
    monkeypatch.setattr(hook, "recent_model_returns", lambda as_of, model: calm_two_fund_returns())
    monkeypatch.setitem(rm.MODEL_PATHS, "regime_hmm_v2", JOINT)
    monkeypatch.setattr(hook, "DEFAULT_MODEL_VERSION", "regime_hmm_v1")
    v1 = hook.decide(daily.index, 21, AS_OF, "historical").record()
    assert v1["sampler"] == "regime_hmm_v1"
    monkeypatch.setattr(hook, "DEFAULT_MODEL_VERSION", "regime_hmm_v2")
    v2 = hook.decide(daily.index, 21, AS_OF, "historical").record()
    assert v2["sampler"] == "regime_hmm_v2"
    again_v1 = hook.decide(daily.index, 21, AS_OF, "historical", forced=v1)
    assert again_v1.active and again_v1.record()["model_sha256"] == v1["model_sha256"]
    assert again_v1.record()["start_probs"] == v1["start_probs"]
    again_v2 = hook.decide(daily.index, 21, AS_OF, "historical", forced=v2)
    assert again_v2.active and again_v2.record()["model_sha256"] == v2["model_sha256"]


def test_the_report_and_chat_describe_a_four_state_two_fund_model():
    from egoquant.advisor.context import _regime_facts
    from egoquant.report import _regime_step
    rec = {"sampler": "regime_hmm_v2", "model_version": "regime_hmm_v2", "model_sha256": "f" * 64,
           "fit_start": "2007-06-22", "fit_end": "2026-10-16", "start_probs": [0.7, 0.2, 0.08, 0.02],
           "regime_today": "calm", "regime_today_probability": 0.7, "model_funds": ["SPY", "AGG"],
           "state_names": ["calm", "normal", "stressed", "crisis"], "long_crisis_share": 0.3}
    text = _regime_step(rec).text
    assert "the S&P 500 fund SPY and the US bond fund AGG" in text
    assert "one of four market regimes: calm, normal, stressed or crisis." in text
    facts = _regime_facts(rec)
    assert facts["model"].startswith("4-state Gaussian Hidden Markov Model") and "SPY and AGG" in facts["model"]
    assert set(facts["regime_probabilities_today"]) == {"calm", "normal", "stressed", "crisis"}


def test_a_near_certain_regime_is_shown_as_the_report_shows_it_never_as_1_or_100_percent():
    """The facts carry "more than 99%" (the report's words), not 0.9999 or 1.0. (Number grounding itself is not changed
    here: it reads any value <= 1.5 as a possible probability, so the 1 in "less than 1%" can still support "100%";
    the pre-existing grounding gap noted in the PR.)"""
    from egoquant.advisor.context import _regime_facts
    rec = {"sampler": "regime_hmm_v2", "model_version": "regime_hmm_v2", "model_sha256": "f" * 64,
           "fit_start": "2007-06-22", "fit_end": "2026-10-16", "regime_today": "normal",
           "regime_today_probability": 0.99998, "start_probs": [0.000007, 0.99998, 0.000003, 0.00001],
           "model_funds": ["SPY", "AGG"], "state_names": ["calm", "normal", "stressed", "crisis"],
           "long_crisis_share": 0.3, "long_crisis_in_100": 30}
    mr = _regime_facts(rec)
    assert mr["regime_today_probability"] == mr["regime_today_probability_as_shown"] == "more than 99%"
    assert set(mr["regime_probabilities_today"].values()) == {"less than 1%", "more than 99%"}
    assert _regime_facts({**rec, "regime_today_probability": 0.8312})["regime_today_probability"] == 0.8312
