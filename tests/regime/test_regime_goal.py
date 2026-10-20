"""The regime sampler inside the goal Monte Carlo: flag off = byte-identical, flag on = deterministic and recorded,
every fallback keeps the request working (egoquant/regime/hook.py, agents/quant/goal.py, agents/quant/agent.py)."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from egoquant.agents.quant import goal as gq
from egoquant.regime import hook
from egoquant.regime.sampler import RegimeSampler
from tests.regime.conftest import AS_OF, CANDIDATES, FEES, VOLS

SPEC = gq.GoalSpec(start_amount=100_000, monthly_flow=500, goal_amount=250_000, horizon_years=15)


def run(daily, sampler=None, n_paths=4000):
    return gq.analyze_goal(daily, CANDIDATES, FEES, VOLS, SPEC, n_paths=n_paths, block_days=21, seed=20261017,
                           tie=0.01, sampler=sampler)


# ==================================================================================================================
# Flag off: exactly the old behaviour
# ==================================================================================================================
def _golden_daily():
    """The fixed input the golden values below were recorded on, with the code of october18 3c9ffe2 (before the
    regime sampler existed; numpy 2.5.4, pandas 3.0.6, Python 3.15)."""
    import pandas as pd
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2008-01-02", periods=3000)
    m = rng.standard_t(4, size=3000) * 0.009 + 0.0003
    return pd.DataFrame({"EQ": m + rng.normal(0, 0.004, 3000), "BD": 0.0001 + 0.2 * rng.normal(0, 0.003, 3000) - 0.05 * m,
                         "GL": 0.0002 + rng.normal(0, 0.01, 3000)}, index=idx)


# Byte-exact goldens are platform specific: the last bits of BLAS / libm results differ between builds (the input hash
# fingerprints goal.model_dump_json(), and two of Agent B's 20 shortlisted candidates sit on the comfort-band edge).
# The code produced both records below unchanged since eacb9cf; a code change matches neither. Add a record for a new
# platform only after checking that the outputs did not change there for a code reason.
GOLDEN_ANALYZE = {
    "recorded (Python 3.15)": {"sha256": "fc43e6833202ef0849cb9c13109bb69546f4740180a67673613e37086f3adb40"},
    "macOS arm64, Python 3.14": {"sha256": "e0fc0f01505912b8251d4504e5ee20a6c572b7d58784b9da8cb680f667993e6b"},
}
GOLDEN_ANALYZE_PROBS = [0.442, 0.26, 0.19725]
# The report and HTML hashes cover the report's WORDS. They were re-recorded on Windows x64 (Python 3.13 and 3.15 give the
# same numbers) after the plain-language pass and the limit-source wording of 20 Oct; the macOS record below still holds the earlier wording and must be
# re-recorded on that platform (python -c "see tests/regime/test_regime_goal.py") before it can match again.
GOLDEN_PIPELINE = {
    "recorded (Python 3.15)": {
        "input_hash": "80b63000ba896ea7",
        "probs": [0.5795, 0.589, 0.5605, 0.535, 0.5135, 0.5055, 0.474, 0.4725, 0.453, 0.449, 0.4445, 0.436, 0.4035,
                  0.392, 0.376, 0.362, 0.3495, 0.346, 0.3395, 0.3],
        "report_sha256": "6b6b87f4a4c8bb644a42abbd4fa6de5b8201a3618db96cc9a077e25b83867d2d",
        "html_sha256": "86e8db720c789f66e5592c1dd97e89e946a738fb569283c9a95fb9559f96ab0a"},
    "macOS arm64, Python 3.14": {
        "input_hash": "29e7a0e6ff1ab9e4",
        "probs": [0.5795, 0.5605, 0.554, 0.535, 0.5135, 0.474, 0.46, 0.453, 0.449, 0.4445, 0.436, 0.4255, 0.4035,
                  0.392, 0.376, 0.362, 0.3495, 0.346, 0.3395, 0.3],
        "report_sha256": "22fe163845bc65321c0c46207fa69552115b5f51b8835bf7e25d455d9d8afa0b",
        "html_sha256": "2c669c483ff083fa77f496b7c6ec806e9c807e813bcc647e7b39cd81e1d3b54b"},
}
GOLDEN_PIPELINE_PROBS = [g["probs"] for g in GOLDEN_PIPELINE.values()]


@pytest.mark.parametrize("flag", ["0", "off", "false", "no", ""])
def test_flag_off_golden_analyze_goal_is_byte_identical(monkeypatch, flag):
    monkeypatch.setenv(hook.FLAG, flag)
    daily = _golden_daily()
    dec = hook.decide(daily.index, 21, AS_OF, "historical")
    assert dec is None
    g = gq.analyze_goal(daily, CANDIDATES, FEES, VOLS, SPEC, n_paths=4000, block_days=21, seed=20261017, tie=0.01,
                        sampler=dec)
    assert [o.prob_goal for o in g.outcomes] == GOLDEN_ANALYZE_PROBS and g.chosen_index == 0
    dump = g.model_dump_json()
    assert '"sampler"' not in dump and "sampler" not in g.model_dump()
    assert {"sha256": hashlib.sha256(dump.encode()).hexdigest()} in GOLDEN_ANALYZE.values()


def test_flag_off_golden_pipeline_probabilities_input_hash_report_and_html(flag_off, monkeypatch):
    # The golden values predate the bond / cash rate anchor (goal.RATE_ANCHOR), a separate, deliberate model change
    # with its own tests (tests/agent_a/test_goal_rate_anchor.py); here it is off so this test keeps pinning
    # "regime flag off = the output from before the regime sampler existed".
    monkeypatch.setattr(gq, "RATE_ANCHOR", False)
    from egoquant.report import render_html
    from tests.helpers import make_answers
    from tests.system.test_goal_based import run as run_goal
    r = run_goal(make_answers(gl_score=25))
    assert r.result.final.engine_version == "0.3.0" and r.goal_analysis.sampler is None
    got = {"input_hash": r.result.final.input_hash, "probs": [o.prob_goal for o in r.goal_analysis.outcomes],
           "report_sha256": hashlib.sha256(r.report.model_dump_json().encode()).hexdigest(),
           "html_sha256": hashlib.sha256(render_html(r.report).encode()).hexdigest()}
    assert got in GOLDEN_PIPELINE.values()                         # one whole record, never a mix


def test_flag_is_on_by_default(monkeypatch, daily):
    """Unset flag = the regime sampler (on by default)."""
    monkeypatch.delenv(hook.FLAG, raising=False)
    assert hook.flag_on()
    dec = hook.decide(daily.index, 21, AS_OF, "historical")
    assert dec is not None and dec.active and dec.record()["sampler"] == hook.DEFAULT_MODEL_VERSION


# ==================================================================================================================
# Flag on: the regime sampler runs, is recorded, and is deterministic
# ==================================================================================================================
def test_flag_on_runs_the_regime_sampler_and_records_it(daily, flag_on):
    dec = hook.decide(daily.index, 21, AS_OF, "historical")
    assert dec is not None and dec.active
    g = run(daily, dec)
    rec = g.sampler
    assert rec["sampler"] == "regime_hmm_v2" and rec.get("fallback_reason") is None
    default = hook.load_regime_model(hook.model_path_for(hook.DEFAULT_MODEL_VERSION))
    assert rec["model_sha256"] == default.sha256 and rec["block_label"] == "first_day"
    assert abs(sum(rec["start_probs"]) - 1) < 1e-9 and rec["regime_today"] == "calm"
    assert 0 <= rec["long_crisis_share"] <= 1 and rec["long_crisis_in_100"] == hook.in_100(rec["long_crisis_share"])
    assert min(rec["pool_blocks"]) >= hook.MIN_POOL_BLOCKS
    assert rec["period_check_samplers"] == ["regime_hmm_v2", "regime_hmm_v2"]
    assert '"sampler"' in g.model_dump_json()


def test_flag_on_same_input_same_seed_identical_results(daily, flag_on):
    a = run(daily, hook.decide(daily.index, 21, AS_OF, "historical"))
    b = run(daily, hook.decide(daily.index, 21, AS_OF, "historical"))
    assert a.model_dump_json() == b.model_dump_json()


def test_flag_on_changes_the_draws_but_not_the_machinery(daily, flag_on):
    old = run(daily)
    new = run(daily, hook.decide(daily.index, 21, AS_OF, "historical"))
    assert [o.menu_index for o in old.outcomes] != [] and len(new.outcomes) == len(old.outcomes)
    assert [o.prob_goal for o in new.outcomes] != [o.prob_goal for o in old.outcomes]
    assert new.n_paths == old.n_paths and new.seed == old.seed and len(new.levers) > 0 and len(new.period_checks) == 2


def test_input_hash_part_and_engine_version_name_the_sampler_and_model(daily, flag_on):
    rec = run(daily, hook.decide(daily.index, 21, AS_OF, "historical")).sampler
    part = hook.hash_part(rec)
    assert "regime_hmm_v2" in part and rec["model_sha256"] in part
    assert hook.engine_version(rec, "0.3.0") == f"0.4.0+regime_hmm_v2.{rec['model_sha256'][:12]}"
    assert hook.engine_version(None, "0.3.0") == "0.3.0" and hook.hash_part(None) == ""
    other = {**rec, "start_probs": [0.1, 0.2, 0.7]}
    assert hook.hash_part(other) != part                          # today's regime is part of the fingerprint


# ==================================================================================================================
# Fallbacks: the request always succeeds, fallback_reason says why, the numbers are the old ones
# ==================================================================================================================
def _expect_fallback(daily, dec, reason):
    assert dec is not None and not dec.active
    g = run(daily, dec)
    old = run(daily)
    assert [o.prob_goal for o in g.outcomes] == [o.prob_goal for o in old.outcomes]       # the block bootstrap
    assert g.sampler["sampler"] == "block_bootstrap" and g.sampler["fallback_reason"] == reason
    return g


def test_short_history_falls_back(daily, flag_on):
    short = daily.loc["2022-01-03":]                              # ~4.8 years
    _expect_fallback(short, hook.decide(short.index, 21, AS_OF, "historical"), "short_overlap")


def test_history_outside_the_labelled_range_falls_back(flag_on):
    from tests.regime.conftest import real_dates_daily
    future = real_dates_daily("2027-01-04", "2033-01-03")
    _expect_fallback(future, hook.decide(future.index, 21, AS_OF, "historical"), "short_overlap")


def test_thin_regime_pool_falls_back(daily, flag_on, monkeypatch):
    monkeypatch.setattr(hook, "MIN_POOL_BLOCKS", 10_000)
    _expect_fallback(daily, hook.decide(daily.index, 21, AS_OF, "historical"), "thin_regime_pool")


def test_synthetic_prices_fall_back(daily, flag_on):
    _expect_fallback(daily, hook.decide(daily.index, 21, AS_OF, "synthetic"), "synthetic_data")


def test_exception_inside_the_sampler_falls_back(daily, flag_on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(RegimeSampler, "draws", boom)
    dec = hook.decide(daily.index, 21, AS_OF, "historical")
    assert dec.active                                             # the decision was fine; the draw fails
    g = run(daily, dec)
    assert g.sampler["fallback_reason"] == "sampler_error" and g.sampler["sampler"] == "block_bootstrap"
    assert [o.prob_goal for o in g.outcomes] == [o.prob_goal for o in run(daily).outcomes]


def test_exception_while_deciding_falls_back(daily, flag_on, monkeypatch):
    def boom(*a, **k):
        raise ValueError("injected")
    monkeypatch.setattr(hook, "label_blocks", boom)
    _expect_fallback(daily, hook.decide(daily.index, 21, AS_OF, "historical"), "sampler_error")


def test_exception_after_the_main_draws_is_recorded_as_partial(daily, flag_on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(RegimeSampler, "extend", boom)
    g = run(daily, hook.decide(daily.index, 21, AS_OF, "historical"))
    assert g.sampler["sampler"] == "regime_hmm_v2" and g.sampler["fallback_reason"] == "sampler_error_in_extend"


def test_pipeline_request_succeeds_with_the_flag_on_and_records_the_fallback(flag_on, monkeypatch):
    """End to end on SYNTHETIC prices: the regime sampler must not run on invented data, and the request works."""
    monkeypatch.setattr(gq, "RATE_ANCHOR", False)         # the golden numbers predate the rate anchor (see above)
    from tests.helpers import make_answers
    from tests.system.test_goal_based import run as run_goal
    r = run_goal(make_answers(gl_score=25))
    assert r.goal_analysis.sampler == {**r.goal_analysis.sampler, "sampler": "block_bootstrap",
                                       "fallback_reason": "synthetic_data"}
    assert [o.prob_goal for o in r.goal_analysis.outcomes] in GOLDEN_PIPELINE_PROBS      # numbers unchanged
    assert r.result.final.engine_version == "0.3.0" and r.approved


def test_pipeline_with_the_regime_sampler_names_it_in_input_hash_and_engine_version(flag_on, monkeypatch):
    """Agent A's candidate ranking and the reported chance come from ONE decision: the chosen portfolio's chance is
    the regime simulation's, and the fingerprint and engine version say so."""
    from tests.helpers import make_answers
    from tests.system.test_goal_based import run as run_goal
    real_decide = hook._decide
    monkeypatch.setattr(hook, "_decide", lambda d, b, a, ds, f, m: real_decide(d, b, a, "historical", f, m))
    r = run_goal(make_answers(gl_score=25))
    rec = r.goal_analysis.sampler
    assert rec["sampler"] == "regime_hmm_v2"
    assert r.result.final.engine_version == hook.engine_version(rec, "0.3.0") != "0.3.0"
    assert r.result.final.input_hash not in {g["input_hash"] for g in GOLDEN_PIPELINE.values()}
    assert r.result.final.diagnostics["goal_probability"] == r.goal_analysis.outcomes[0].prob_goal
    assert r.approved                                             # the Risk Engine still has the final say
