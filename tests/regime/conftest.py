"""Shared fixtures for the regime-aware sampler tests (egoquant/regime). No network, no hmmlearn.

The committed artifact egoquant/data/regime_model_v1.json labels real trading days 2007-06-22 .. 2026-10-16, so the
test histories use real business days in that range with seeded made-up returns (labelled "historical" so the regime
sampler is allowed to run; the decision only looks at the dates). Today's regime comes from a fixed series instead of
the local price cache, so every test is deterministic on every machine.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.regime import hook

AS_OF = date(2026, 10, 16)


def real_dates_daily(start: str = "2007-07-02", end: str = "2026-10-16", seed: int = 11) -> pd.DataFrame:
    """Daily returns of three made-up funds on real business days (the regime labels only use the dates)."""
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    m = rng.standard_t(4, size=len(idx)) * 0.009 + 0.0003
    return pd.DataFrame({"EQ": m + rng.normal(0, 0.004, len(idx)),
                         "BD": 0.0001 + rng.normal(0, 0.003, len(idx)) - 0.05 * m,
                         "GL": 0.0002 + rng.normal(0, 0.01, len(idx))}, index=idx)


CANDIDATES = [(0, "a", 0.2, {"EQ": 0.2, "BD": 0.7, "GL": 0.1}), (1, "b", 0.5, {"EQ": 0.5, "BD": 0.4, "GL": 0.1}),
              (2, "c", 0.8, {"EQ": 0.8, "BD": 0.1, "GL": 0.1})]
FEES = {"EQ": 0.001, "BD": 0.0005, "GL": 0.0025}
VOLS = {0: 0.05, 1: 0.1, 2: 0.15}


def calm_recent_returns():
    """A quiet year of SPY-like log returns (for "today's regime")."""
    r = np.random.default_rng(3).normal(0.0004, 0.006, 300)
    return r, AS_OF, 0


def calm_two_fund_returns():
    """A quiet year of SPY-like and AGG-like log returns (today's regime for a two-fund model, e.g. v2 "joint")."""
    rng = np.random.default_rng(5)
    return np.column_stack([rng.normal(0.0004, 0.006, 300), rng.normal(0.0001, 0.002, 300)]), AS_OF, 0


def fixed_recent_returns(monkeypatch):
    """Today's regime from fixed series instead of the local price cache (one-fund and several-fund models)."""
    monkeypatch.setattr(hook, "recent_market_returns", lambda as_of: calm_recent_returns())
    monkeypatch.setattr(hook, "recent_model_returns", lambda as_of, model: calm_two_fund_returns())


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(hook.FLAG, "1")
    fixed_recent_returns(monkeypatch)
    yield


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setenv(hook.FLAG, "0")             # the kill switch (the flag is on by default)
    yield


@pytest.fixture(scope="session")
def daily() -> pd.DataFrame:
    return real_dates_daily()
