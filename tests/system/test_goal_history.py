"""Goal-based path: completing short histories, the minimum-history guard, 100 candidates, the walkthrough.

SYNTHETIC or hand-made data only; no network.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource
from egoquant.agents.quant import goal as gq
from egoquant.demo.personas import PERSONAS
from egoquant.models import AgentConfig
from egoquant.pipeline import run_pipeline

AS_OF = date(2026, 10, 16)


def _prices(n=2000, young_from=1500, beta=1.5, seed=3):
    """A long fund L, a young fund Y = beta * L + noise that only exists from day `young_from`."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    long_r = rng.normal(0.0004, 0.01, n)
    young_r = beta * long_r + rng.normal(0.0, 0.004, n)
    prices = pd.DataFrame({"L": 100 * np.cumprod(1 + long_r), "Y": 50 * np.cumprod(1 + young_r)}, index=idx)
    prices.loc[prices.index[:young_from], "Y"] = np.nan
    return prices


# ==========================================================================
# 1. Completing short histories (Stambaugh 1997 / Page 2013)
# ==========================================================================
def test_a_young_fund_is_completed_from_the_long_fund_it_moves_with():
    daily, notes, note = gq.complete_history(_prices(), block_days=21, seed=7)
    assert daily.index[0] == _prices().index[1] and not daily.isna().any().any()   # the long fund's whole window
    assert [n.ticker for n in notes] == ["Y"] and notes[0].factors == ["L"]
    assert notes[0].betas[0] == pytest.approx(1.5, abs=0.1) and notes[0].r_squared > 0.8
    filled = daily.loc[: pd.Timestamp(notes[0].own_start)].iloc[:-1]
    assert len(filled) == notes[0].filled_days
    assert np.corrcoef(filled["L"], filled["Y"])[0, 1] > 0.85        # the long fund's moves reach the young one
    assert "completed" in note


def test_completion_is_deterministic_and_leaves_full_histories_alone():
    a, _, _ = gq.complete_history(_prices(), 21, 7)
    b, _, _ = gq.complete_history(_prices(), 21, 7)
    pd.testing.assert_frame_equal(a, b)
    full = _prices(young_from=0)
    daily, notes, note = gq.complete_history(full, 21, 7)
    assert notes == [] and "every fund has real data" in note
    pd.testing.assert_frame_equal(daily, full.pct_change().iloc[1:])


def test_the_young_funds_long_run_mean_follows_the_long_window_not_its_short_sample():
    # The young fund's own sample is a strong stretch of the long fund; Stambaugh's estimator pulls its mean
    # towards beta * the long fund's FULL-window mean instead of extending that stretch.
    prices = _prices(young_from=1700)
    daily, notes, _ = gq.complete_history(prices, 21, 7)
    full_long_mean = daily["L"].mean()
    assert daily["Y"].mean() == pytest.approx(notes[0].betas[0] * full_long_mean, abs=0.0004)


# ==========================================================================
# 2. Agent C: the long history comes with the first fetch, each fund its own start
# ==========================================================================
class _CountingSynthetic:
    name = "fake-live"
    data_source = "synthetic"

    def __init__(self, late=None):
        self.inner, self.late, self.calls = SyntheticPriceSource(), late or {}, []

    def needs_request(self, ticker, start, end):
        return True

    def fetch_adjusted_close(self, tickers, start, end):
        self.calls.append((tuple(tickers), start))
        frame = self.inner.fetch_adjusted_close(tickers, start, end)
        for t, first in self.late.items():
            if t in frame.columns:
                frame.loc[: pd.Timestamp(first), t] = np.nan
        return frame


def test_fetch_universe_with_history_serves_the_goal_history_from_memory():
    src = _CountingSynthetic(late={"VTI": "2023-06-01"})
    agent = MarketIntelligenceAgent(InvestorInterests(investor_id="t", interest_areas=["stocks"], max_r_level=3),
                                    source=src, directory=None)
    agent.fetch_universe(AS_OF, 252, history_days=2520)
    calls = len(src.calls)
    history = agent.get_price_history(agent.selection.tickers, AS_OF, 2520)
    assert len(src.calls) == calls                                     # no second fetch
    assert history["BIL"].first_valid_index() < pd.Timestamp("2017-02-01")          # the whole ~10-year window
    assert history["VTI"].first_valid_index() > pd.Timestamp("2023-06-01")     # its own start, not cut away


# ==========================================================================
# 3. Agent A: 100 candidates; the guard against too little history
# ==========================================================================
def _goal_run(persona="growth_investor", config=None):
    p = PERSONAS[persona]
    goal = gq.GoalSpec(start_amount=p.amount_invested, monthly_flow=200.0, goal_amount=p.amount_invested * 1.8,
                       horizon_years=p.horizon_years)
    return run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
                        goal_type=p.goal_type, horizon_years=p.horizon_years, as_of=AS_OF,
                        trading_platforms=p.trading_platforms, price_source=SyntheticPriceSource(), goal=goal,
                        agent_config=config)


def test_agent_a_builds_exactly_the_target_number_of_candidates():
    run = _goal_run("balanced_saver")
    assert len(run.result.menu.portfolios) == AgentConfig().candidate_target == 100
    assert 1 <= len(run.screening.approved) <= 20 and run.goal_analysis.n_paths == 10_000


def test_too_little_history_gives_no_goal_probabilities_but_still_a_portfolio():
    run = _goal_run("balanced_saver", AgentConfig(goal_min_history_days=5000))
    assert run.goal_analysis is None and "needs at least 5000" in run.goal_unavailable
    assert run.approved and run.result.final.weights


# ==========================================================================
# 4. The walkthrough proves its own claims
# ==========================================================================
def test_walkthrough_checks_pass_and_the_page_shows_every_step():
    from egoquant.demo.goal_walkthrough import default_goal, render_html, run_walkthrough

    spec = default_goal("balanced_saver")
    walk = run_walkthrough("balanced_saver", AS_OF, spec, price_source=SyntheticPriceSource())
    assert walk.checks and all(c.passed for c in walk.checks), [c for c in walk.checks if not c.passed]
    names = {c.name for c in walk.checks}
    assert {"Chosen = highest chance of reaching the goal", "Reproducible (same data + seed -> same result)"} <= names
    assert len(walk.paths) == 2 and walk.paths[0]["rows"]
    page = render_html(walk)
    for heading in ("1. The goal", "2. The data", "candidate portfolios", "4. How the Monte Carlo", "5. Results", "6. Checks"):
        assert heading in page


# ==========================================================================
# 5. Zero-alpha backfill and the hindsight correction
# ==========================================================================
def test_a_short_windows_alpha_is_not_carried_into_the_rebuilt_years():
    prices = _prices(young_from=1500)
    young = prices["Y"].pct_change()
    lucky = young.copy()
    lucky.iloc[1500:] += 0.002                                         # +50% a year of "alpha", only in its window
    prices["Y"] = (1 + lucky.fillna(0)).cumprod() * 50
    prices.loc[prices.index[:1500], "Y"] = np.nan
    daily, notes, _ = gq.complete_history(prices, 21, 7)
    rebuilt = daily["Y"].iloc[: notes[0].filled_days]
    assert notes[0].alpha_dropped > 0.3                                # it was there in the short window...
    assert rebuilt.mean() == pytest.approx(notes[0].betas[0] * daily["L"].iloc[: notes[0].filled_days].mean(),
                                           abs=0.0006)                 # ...but not carried back


def test_hindsight_correction_sets_winners_to_beta_times_the_market():
    rng = np.random.default_rng(5)
    n = 3000
    market = rng.normal(0.0003, 0.01, n)
    winner = 1.3 * market + rng.normal(0.0015, 0.015, n)              # a big past winner (alpha ~ +38% a year)
    bond = rng.normal(0.0001, 0.003, n)
    daily = pd.DataFrame({"VT": market, "WIN": winner, "BND": bond}, index=pd.bdate_range("2010-01-01", periods=n))
    adjusted, notes, used = gq.equilibrium_means(daily, {"VT": "equity", "WIN": "equity", "BND": "bond"})
    assert used == "VT" and [m.ticker for m in notes] == ["WIN"]
    beta = float(np.cov(winner, market, bias=True)[0, 1] / market.var())
    assert notes[0].beta == pytest.approx(beta, abs=1e-3)
    assert adjusted["WIN"].mean() == pytest.approx(beta * market.mean(), abs=1e-12)
    assert np.allclose(adjusted["WIN"] - adjusted["WIN"].mean(), daily["WIN"] - daily["WIN"].mean())   # same moves
    pd.testing.assert_series_equal(adjusted["VT"], daily["VT"])        # the market and bonds untouched
    pd.testing.assert_series_equal(adjusted["BND"], daily["BND"])
    unchanged, none, _ = gq.equilibrium_means(daily[["WIN", "BND"]], {"WIN": "equity", "BND": "bond"})
    assert none == [] and unchanged.equals(daily[["WIN", "BND"]])      # no market proxy -> no change


def test_a_rate_limit_on_a_fund_cached_for_the_short_window_still_uses_its_cached_prices():
    """The long fetch hits the hourly limit; the fund is on disk for the analysis window: keep it (from disk)."""
    from egoquant.agents.market_intel import RateLimitError

    class _ShortCached(_CountingSynthetic):
        def needs_request(self, ticker, start, end):
            return start < pd.Timestamp("2020-01-01").date()            # cached only for recent windows

        def fetch_adjusted_close(self, tickers, start, end):
            if start < pd.Timestamp("2020-01-01").date() and tickers != ["BIL"]:
                raise RateLimitError("Tiingo rate limit reached (HTTP 429)")
            return super().fetch_adjusted_close(tickers, start, end)

    agent = MarketIntelligenceAgent(InvestorInterests(investor_id="t", interest_areas=["stocks"], max_r_level=3),
                                    source=_ShortCached(), directory=None)
    report = agent.fetch_universe(AS_OF, 252, budget={"us_listed": 40}, history_days=2520)
    assert "VTI" in agent.selection.tickers and report.rate_limited == ["us_listed"]


def test_a_transient_error_on_one_ticker_skips_it_without_switching_the_run():
    from egoquant.agents.market_intel import PriceSourceError

    class _Flaky(_CountingSynthetic):
        def fetch_adjusted_close(self, tickers, start, end):
            if tickers == ["VTI"]:
                raise PriceSourceError("Tiingo could not be reached (network error: ReadTimeout)")
            return super().fetch_adjusted_close(tickers, start, end)

    agent = MarketIntelligenceAgent(InvestorInterests(investor_id="t", interest_areas=["us_stocks"], max_r_level=3),
                                    source=_Flaky(), directory=None)
    report = agent.fetch_universe(AS_OF, 252)
    assert agent.fallback_reason is None and isinstance(agent.source, _Flaky)      # still the live feed
    assert "VTI" not in agent.selection.tickers and report.replaced.get("VTI")     # same-exposure fund instead


def test_an_unreachable_host_switches_the_whole_run_after_two_failures_in_a_row():
    """A dead network must cost seconds, not one timeout per candidate (hundreds) behind the app's request limit."""
    from egoquant.agents.market_intel import PriceSourceError, SyntheticPriceSource

    class _Dead:
        name = "dead-live"
        data_source = "historical"

        def __init__(self):
            self.calls = []

        def needs_request(self, ticker, start, end):
            return True

        def fetch_adjusted_close(self, tickers, start, end):
            self.calls.append(tickers)
            raise PriceSourceError("Tiingo could not be reached (network error: ConnectTimeout)")

    dead = _Dead()
    agent = MarketIntelligenceAgent(InvestorInterests(investor_id="t", interest_areas=["stocks", "bonds"],
                                                      max_r_level=3),
                                    source=dead, directory=None, fallback_to_synthetic=True, use_snapshot=False)
    agent.fetch_universe(AS_OF, 252)
    assert len(dead.calls) == 2                                    # not one per candidate
    assert isinstance(agent.source, SyntheticPriceSource) and "could not be reached" in agent.fallback_reason
