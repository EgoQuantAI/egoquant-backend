"""The slow-feed drill (eval/slow_feed_drill.py): a /portfolio build with a live price feed that answers correctly but costs
15 simulated seconds per request must finish within 30 simulated seconds on the committed snapshot, say so, and still give a
real, approved portfolio. No network, no real waiting; each scenario is its own process (about 8 real seconds).

Until Agent C's slow-feed guard is in the tree (`MarketIntelligenceAgent(clock=...)`), the scenario runs are an expected
failure (strict): the build waits for every core fund and holding, 60 s for the minimal case and 15 s more for each holding.
The day the guard lands the condition turns false and they must pass, with nothing to remove by hand.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

from egoquant.agents.market_intel.agent import MarketIntelligenceAgent

ROOT = Path(__file__).resolve().parents[1]
HAS_GUARD = "clock" in inspect.signature(MarketIntelligenceAgent.__init__).parameters


def load():
    spec = importlib.util.spec_from_file_location("slow_feed_drill", ROOT / "eval" / "slow_feed_drill.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = load()


def result(**kw) -> dict:
    base = dict(scenario="x", what="x", simulated_seconds=30.0, live_requests=2, live_tickers=["BIL", "AGG"], approved=True,
                status="optimal", data_source="historical", fallback_reason="the price feed answered too slowly",
                banners=["LIVE PRICES UNAVAILABLE: this portfolio uses saved real prices as of 2026-10-16, cached."],
                weights_sum=1.0, n_holdings=20, universe=40, expect_live_requests=None)
    base.update(kw)
    return base


def states(checks) -> dict:
    return {name: state for name, state, _ in checks}


# ---- the judging, without running a build -------------------------------------------------------------------------
def test_a_build_that_falls_back_quickly_and_says_so_passes():
    checks = drill.judge(result())
    assert drill.verdict(checks) == "PASS" and set(states(checks).values()) == {"PASS"}


@pytest.mark.parametrize("change, failing", [
    (dict(simulated_seconds=30.1), "finishes within the simulated budget"),
    (dict(simulated_seconds=60.0, live_requests=4, live_tickers=["BIL", "AGG", "SHY", "VT"]), "finishes within the simulated budget"),
    (dict(live_requests=3), "waits for at most two live requests"),
    (dict(banners=[]), "falls back to the snapshot and says so (amber banner)"),
    (dict(banners=["LIVE PRICES UNAVAILABLE: this portfolio uses saved real prices as of 2026-09-01"]),
     "falls back to the snapshot and says so (amber banner)"),
    (dict(universe=4), "prices the investor's choices, not only the 4 core funds"),
    (dict(data_source="synthetic"), "the data is real, never synthetic"),
    (dict(approved=False), "the portfolio is real and approved"),
    (dict(weights_sum=0.5), "the portfolio is real and approved"),
])
def test_each_way_a_build_can_go_wrong_fails_its_own_check(change, failing):
    checks = drill.judge(result(**change))
    assert states(checks)[failing] == "FAIL" and drill.verdict(checks) == "FAIL"


def test_the_budget_is_exactly_two_requests_and_the_plain_label_only_warns():
    assert drill.BUDGET_SECONDS == 30.0 and drill.REQUEST_SECONDS == 15.0
    assert drill.verdict(drill.judge(result(simulated_seconds=30.0))) == "PASS"
    checks = drill.judge(result(fallback_reason="data vendor error"))
    assert states(checks)["the API's plain label names the slow feed"] == "WARN" and drill.verdict(checks) == "PASS"
    assert states(drill.judge(result(fallback_reason=None)))["the API's plain label names the slow feed"] == "WARN"


def test_snapshot_price_mode_must_make_no_live_request_at_all():
    quiet = result(expect_live_requests=0, simulated_seconds=0.0, live_requests=0, live_tickers=[], fallback_reason=None,
                   banners=["Prices: saved snapshot of real prices as of 16 Oct 2026, Yahoo Finance."])
    checks = drill.judge(quiet)
    assert drill.verdict(checks) == "PASS" and "makes 0 live request(s)" in states(checks)
    busy = drill.judge({**quiet, "live_requests": 4, "live_tickers": ["BIL"] * 4, "simulated_seconds": 60.0})
    assert states(busy)["makes 0 live request(s)"] == "FAIL"


def test_the_scenarios_cover_the_cases_of_the_brief():
    assert set(drill.SCENARIOS) == {"app_flow_with_goal", "twenty_areas", "twenty_areas_v2", "holdings", "snapshot_mode"}
    assert len(drill.TWENTY_AREAS) == 20 and len(set(drill.TWENTY_AREAS)) == 20
    assert drill.SCENARIOS["twenty_areas_v2"]["env"] == {"EGOQUANT_PORTFOLIO_METHOD": "v2"}
    assert drill.SCENARIOS["snapshot_mode"]["env"] == {"EGOQUANT_PRICE_MODE": "snapshot"}
    assert abs(sum(drill.HOLDINGS.values()) - 1.0) < 1e-9


# ---- the drill itself: real builds on a fake slow feed -------------------------------------------------------------
@pytest.mark.xfail(not HAS_GUARD, strict=True, reason="Agent C's slow-feed guard (fix/damla-3) is not in this tree yet")
@pytest.mark.parametrize("name", ["app_flow_with_goal", "twenty_areas", "twenty_areas_v2", "holdings", "snapshot_mode"])
def test_a_build_on_a_slow_feed_finishes_in_the_simulated_budget_on_the_snapshot(name):
    outcome = drill.run_in_subprocess(name)
    assert "error" not in outcome, outcome
    checks = drill.judge(outcome)
    failing = [(c, d) for c, state, d in checks if state == "FAIL"]
    assert not failing, failing
