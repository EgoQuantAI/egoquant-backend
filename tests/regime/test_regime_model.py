"""The regime model artifact and the numpy HMM arithmetic (egoquant/regime/model.py)."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from egoquant.regime import hook
from egoquant.regime.model import (
    MODEL_PATH,
    RegimeModelError,
    block_transition_matrix,
    content_hash,
    current_regime_probs,
    load_regime_model,
    stationary_distribution,
)
from tests.regime.conftest import AS_OF


@pytest.fixture(scope="module")
def body() -> dict:
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def write(tmp_path, body, rehash=True, name="model.json"):
    if rehash:
        body = {**body, "sha256": content_hash(body)}
    p = tmp_path / name
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# ==================================================================================================================
# The committed artifact
# ==================================================================================================================
def test_committed_artifact_is_valid_and_sorted_calm_stressed_crisis(body):
    m = load_regime_model()
    assert m.version == "regime_hmm_v1" and m.sha256 == body["sha256"] == content_hash(body)
    assert m.transition.shape == (3, 3) and np.allclose(m.transition.sum(axis=1), 1.0, atol=1e-12)
    assert m.means.shape == (3, 2) and m.covariances.shape == (3, 2, 2)
    vols = [s["ann_vol"] for s in body["state_summary"]]
    assert vols == sorted(vols) and body["state_names"] == ["calm", "stressed", "crisis"]
    assert body["n_components"] == 3 and body["covariance_type"] == "full" and body["random_state"] == 20261017
    assert body["labels"]["n_days"] == sum(body["labels"]["counts"])


def test_known_crises_are_labelled_crisis():
    m = load_regime_model()
    days = np.array(["2008-10-15", "2020-03-16", "2017-06-15", "2026-01-07"], dtype="datetime64[D]")
    labels = m.labels_on(days)
    assert labels[0] == 2 and labels[1] == 2                # Lehman aftermath, COVID crash
    assert labels[2] == 0                                   # a famously quiet market


def test_labels_outside_the_range_are_minus_one_and_weekends_take_the_last_trading_day():
    m = load_regime_model()
    out = m.labels_on(np.array(["2005-01-03", "2030-01-02"], dtype="datetime64[D]"))
    assert list(out) == [-1, -1]
    fri, sat = m.labels_on(np.array(["2020-03-13", "2020-03-14"], dtype="datetime64[D]"))
    assert fri == sat


# ==================================================================================================================
# Validation: a broken artifact is refused (and the sampler falls back, never raises)
# ==================================================================================================================
def test_missing_file(tmp_path):
    with pytest.raises(RegimeModelError) as err:
        load_regime_model(tmp_path / "nope.json")
    assert err.value.reason == "artifact_missing"


def test_corrupted_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"model_version": "regime_hmm_v1", "transition', encoding="utf-8")
    with pytest.raises(RegimeModelError) as err:
        load_regime_model(p)
    assert err.value.reason == "artifact_invalid"


def test_wrong_hash(tmp_path, body):
    tampered = {**body, "transition_matrix": [[0.98, 0.02, 0.0], [0.02, 0.97, 0.01], [0.0, 0.02, 0.98]]}
    with pytest.raises(RegimeModelError) as err:
        load_regime_model(write(tmp_path, tampered, rehash=False))
    assert err.value.reason == "artifact_hash_mismatch"


def test_rows_that_do_not_sum_to_one(tmp_path, body):
    bad = {**body, "transition_matrix": [[0.9, 0.05, 0.0], [0.02, 0.97, 0.01], [0.0, 0.02, 0.98]]}
    with pytest.raises(RegimeModelError) as err:
        load_regime_model(write(tmp_path, bad))                 # hash recomputed: the semantic check must catch it
    assert err.value.reason == "artifact_invalid"


@pytest.mark.parametrize("field, value", [
    ("transition_matrix", [[0.5, 0.5], [0.5, 0.5]]),
    ("means", [[0.0, 0.0], [1.0, 1.0]]),
    ("covariances", [[[1.0, 0.0], [0.0, 1.0]]] * 2),
    ("start_probs", [0.5, 0.5]),
    ("covariances", [[[1.0, 2.0], [2.0, 1.0]]] * 3),           # not positive definite
])
def test_wrong_shapes_or_impossible_values(tmp_path, body, field, value):
    with pytest.raises(RegimeModelError) as err:
        load_regime_model(write(tmp_path, {**body, field: value}))
    assert err.value.reason == "artifact_invalid"


@pytest.mark.parametrize("break_it", ["missing", "corrupt", "hash", "rows", "shape"])
def test_every_broken_artifact_means_fallback_not_exception(tmp_path, body, daily, flag_on, break_it):
    if break_it == "missing":
        path = tmp_path / "missing.json"
    elif break_it == "corrupt":
        path = tmp_path / "corrupt.json"
        path.write_text("not json", encoding="utf-8")
    elif break_it == "hash":
        path = write(tmp_path, {**body, "n_days": 1}, rehash=False)
    elif break_it == "rows":
        path = write(tmp_path, {**body, "transition_matrix": [[1.0, 1.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]})
    else:
        path = write(tmp_path, {**body, "means": [[0.0]]})
    dec = hook.decide(daily.index, 21, AS_OF, "historical", model_path=path)
    rec = dec.record()
    assert not dec.active and rec["sampler"] == "block_bootstrap"
    assert rec["fallback_reason"] in {"artifact_missing", "artifact_invalid", "artifact_hash_mismatch"}
    assert dec.draws(len(daily) - 20, 100, 12, 1) is None       # the caller then draws exactly as before


# ==================================================================================================================
# HMM arithmetic
# ==================================================================================================================
def test_stationary_distribution_is_invariant():
    m = load_regime_model()
    pi = stationary_distribution(m.transition)
    assert np.allclose(pi @ m.transition, pi, atol=1e-10) and abs(pi.sum() - 1) < 1e-12 and (pi > 0).all()


def test_block_transition_is_the_21st_matrix_power():
    m = load_regime_model()
    p21 = block_transition_matrix(m.transition, 21)
    assert np.allclose(p21, np.linalg.matrix_power(m.transition, 21), atol=1e-12)
    assert np.allclose(p21.sum(axis=1), 1.0)
    assert np.allclose(block_transition_matrix(m.transition, 1), m.transition)


def test_current_regime_reads_the_data():
    m = load_regime_model()
    rng = np.random.default_rng(0)
    calm, _ = current_regime_probs(rng.normal(0.0005, 0.005, 300), m)
    crisis, _ = current_regime_probs(np.r_[rng.normal(0.0005, 0.005, 200), rng.normal(-0.004, 0.035, 60)], m)
    assert np.argmax(calm) == 0 and calm[0] > 0.9
    assert np.argmax(crisis) == 2 and crisis[2] > 0.9


def test_without_recent_data_the_stationary_distribution_is_used():
    m = load_regime_model()
    probs, source = current_regime_probs(None, m)
    assert source == "stationary" and np.allclose(probs, stationary_distribution(m.transition))
    probs, source = current_regime_probs(np.zeros(5), m)        # too short for the 21-day volatility
    assert source == "stationary"


def test_current_regime_is_deterministic_and_moves_toward_the_long_run_when_data_is_old():
    m = load_regime_model()
    r = np.random.default_rng(5).normal(0.0004, 0.006, 300)
    a, _ = current_regime_probs(r, m)
    b, _ = current_regime_probs(r.copy(), m)
    assert np.array_equal(a, b)
    later, _ = current_regime_probs(r, m, steps_ahead=500)
    pi = stationary_distribution(m.transition)
    assert np.abs(later - pi).sum() < np.abs(a - pi).sum()


def test_recent_market_returns_reads_only_local_files(tmp_path, monkeypatch):
    from egoquant.regime import model as rm
    spy = {"ticker": "SPY", "start": "2026-01-02", "end": "2026-10-16", "fetched_on": "2026-10-18",
           "prices": {str(d.date()): 100 + i * 0.1 for i, d in enumerate(__import__("pandas").bdate_range("2026-01-02", "2026-10-16"))}}
    (tmp_path / "tiingo").mkdir()
    (tmp_path / "tiingo" / "SPY.json").write_text(json.dumps(spy), encoding="utf-8")
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("EGOQUANT_PRICE_SNAPSHOT", "off")
    returns, last, gap = rm.recent_market_returns(date(2026, 10, 16))
    assert last == date(2026, 10, 16) and gap == 0 and len(returns) == len(spy["prices"]) - 1
    _, last, gap = rm.recent_market_returns(date(2026, 10, 21))   # Wednesday after: 3 weekdays later
    assert gap == 3
    assert rm.recent_market_returns(date(2025, 1, 1)) is None     # nothing before as_of: no data
