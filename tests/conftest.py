"""Shared pytest setup.

Test layout (one folder per component):
    tests/risk_profile/   questionnaire scoring, adapter, C-level table
    tests/agent_a/        Agent A math core, the agent itself, the backtest harness
    tests/agent_b/        Agent B (behavioral twin)
    tests/agent_c/        Agent C (universe selection, sources, cache, snapshots)
    tests/system/         pipeline, Risk Engine and client report (whole chain, SYNTHETIC prices)
    tests/live/           demo trader profiles on REAL market data (Tiingo + Binance), marked `live`

Everything except tests/live runs offline. Skip the live tests with:  pytest -m "not live"
"""

from __future__ import annotations

import os

# One math-library thread, the same default as main.py (20 Oct): float results, and with them the golden hashes of
# tests/regime/test_regime_goal.py, must not depend on how many BLAS threads the machine has. Set before numpy is imported.
for _threads in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_threads, "1")

import pytest  # noqa: E402
import requests  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: needs network access to Tiingo and Binance (real market data)")
    # The background price-cache builder (market_intel/warm.py) starts with the app; tests must never let
    # it spend real Tiingo requests. Tests of the builder construct it directly.
    import os
    os.environ["EGOQUANT_WARM"] = "off"
    # Same for the briefing's background tone worker (briefing/web.py): never real OpenAI calls from tests.
    os.environ["EGOQUANT_BRIEFING_TONE_BACKGROUND"] = "off"
    os.environ["EGOQUANT_NEWS_WORKER"] = "off"                  # tests enable it explicitly where they test it
    # /debug/portfolio is off in production; tests use it to get a sealed context (tests/test_debug_gate.py checks it off)
    os.environ.setdefault("EGOQUANT_DEBUG_PAGES", "on")


@pytest.fixture(scope="session")
def live_network() -> None:
    """Skip (not fail) without a Tiingo key or when the market-data hosts cannot be reached."""
    from egoquant import settings
    if not settings.tiingo_api_key():
        pytest.skip("TIINGO_API_KEY is not set (see .env.example)")
    for url in ("https://api.tiingo.com", "https://data-api.binance.vision/api/v3/ping"):
        try:
            requests.get(url, timeout=10)
        except requests.RequestException as exc:
            pytest.skip(f"no network access to {url}: {exc}")
