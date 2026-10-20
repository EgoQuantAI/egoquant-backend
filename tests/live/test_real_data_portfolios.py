"""Demo trader profiles end to end on REAL market data. Run with:  pytest -v tests/live

Each persona (egoquant/demo/personas.py) goes through the whole chain: questionnaire -> Agent C (real
prices: Tiingo for US-listed funds, Binance for crypto spot) -> Agent A (+ Agent B) -> Risk Engine ->
client report. The decision date is the last weekday before today, so the portfolio is built from the
latest closed prices. Prices are cached on disk (egoquant.settings.cache_dir), so a rerun on the same
day makes no new requests.

The assertions are properties every proposal must have whatever the market did, not fixed numbers.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG, PriceSourceError, build_live_source
from egoquant.agents.market_intel.agent import (
    DEFAULT_MAX_CRYPTO,
    DEFAULT_MAX_INSTRUMENTS,
    DEFAULT_MAX_PER_AREA,
    DEFAULT_MAX_SINGLE_STOCKS,
)
from egoquant.cli import last_weekday_before
from egoquant.demo.personas import PERSONAS, run_persona
from egoquant.models import CLASS_CAP_FIELDS, DEFAULT_RISK_CLASS_POLICIES
from egoquant.report import render_html
from egoquant.scenarios import CAPACITY_SCENARIO

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_network")]

AS_OF = last_weekday_before(date.today())
_RUNS: dict = {}


def run(key: str):
    """One real-data run per persona per test session. LIVE feeds only: the committed price snapshot
    (Agent C's fallback) is switched off, so a rate limit skips instead of silently using it."""
    if key not in _RUNS:
        with skip_on_rate_limit(), pytest.MonkeyPatch.context() as mp:
            mp.setenv("EGOQUANT_PRICE_SNAPSHOT", "off")
            _RUNS[key] = run_persona(key, AS_OF)
    return _RUNS[key]


@contextmanager
def skip_on_rate_limit():
    """Tiingo's free tier allows 50 requests an hour. Hitting it is not a bug: skip, and the prices
    fetched so far stay cached, so a rerun later continues from there."""
    try:
        yield
    except PriceSourceError as exc:
        if "rate limit" in str(exc):
            pytest.skip(f"market-data rate limit: {exc}")
        raise


@pytest.mark.parametrize("key", list(PERSONAS))
def test_persona_gets_an_approved_portfolio_from_real_prices(key):
    out, persona = run(key), PERSONAS[key]
    final, market = out.result.final, out.market_report
    assert out.profile.c_level == persona.expected_class
    assert final.status in ("optimal", "fallback") and out.approved, out.review.violations
    assert final.data_source == "historical" and "tiingo" in market.source
    assert market.last_date >= AS_OF - timedelta(days=7)                # fresh prices, nothing after as_of
    assert market.last_date <= AS_OF
    assert sum(final.weights.values()) == pytest.approx(1.0)
    assert out.report.headline.startswith("We propose")
    assert len(out.report.interest_notes) == len(out.selection.interest_areas)
    assert "DEMO: this report uses synthetic" not in " ".join(out.report.banners)
    render_html(out.report)


@pytest.mark.parametrize("key", list(PERSONAS))
def test_persona_portfolio_respects_every_c_level_rule(key):
    out = run(key)
    final, profile = out.result.final, out.profile
    policy = DEFAULT_RISK_CLASS_POLICIES[profile.c_level]
    held = {a: w for a, w in final.weights.items() if w > 1e-6}
    classes = {i.ticker: i.asset_class for i in out.selection.instruments}
    assert set(held) <= set(out.selection.tickers)
    # R-levels come from Agent C's catalog for this run (curated catalog + the directory's funds)
    assert all(out.agent_c.instruments[a].r_level <= profile.max_r_level for a in held)   # what can be traded
    weight = lambda cls: sum(w for a, w in held.items() if classes[a] == cls)
    assert weight("equity") <= min(policy.max_equity, profile.suitability_max_equity) + 1e-6
    min_cash = max(out.context.min_cash_buffer, out.context.liquidity_need)
    for cls, field in CLASS_CAP_FIELDS.items():
        cap = getattr(policy, field)
        assert weight(cls) <= (max(cap, min_cash) if cls == "cash" else cap) + 1e-6, cls
    crash_loss = -final.stress[CAPACITY_SCENARIO]
    assert crash_loss <= policy.max_stress_loss + 1e-6
    assert crash_loss <= out.context.max_acceptable_loss + 1e-6


def test_no_loss_saver_holds_only_what_does_not_fall():
    out = run("no_loss_saver")
    assert out.profile.no_loss_gate
    assert out.result.final.stress[CAPACITY_SCENARIO] >= -1e-9
    assert all(out.agent_c.instruments[t].r_level == 1 for t in out.selection.tickers)


def test_balanced_saver_gets_what_she_picked_and_agent_b_limit_is_respected():
    out = run("balanced_saver")
    # Gold, US and dividend funds: the most traded fund of each exposure group (e.g. GLD rather than IAU)
    for area in ("gold", "us_stocks", "dividend_stocks"):
        assert any(area in out.agent_c.instruments[t].areas for t in out.selection.tickers), area
    assert any(out.agent_c.instruments[t].asset_class == "commodity" and "gold" in out.agent_c.instruments[t].areas
               for t in out.selection.tickers)
    real_estate = next(o for o in out.preference_form.question("P1").options if o.id == "real_estate")
    assert not real_estate.available and "R4" in real_estate.reason             # REITs are R4 > R3
    limit = next(c for c in out.result.behavioral_constraints if c.type == "max_stress_loss")
    if limit.label not in out.result.final.ignored_constraints:
        assert -out.result.final.stress[limit.scenario] <= limit.value + 1e-6


def test_crypto_trader_is_priced_from_both_venues():
    out = run("crypto_trader")
    sources = out.market_report.price_sources
    assert sources["BTCUSDT"] == "binance" and sources["IBIT"] == "tiingo"
    assert {"IBIT", "BTCUSDT", "ETHUSDT"} <= set(out.selection.tickers)
    assert any(out.agent_c.instruments[t].asset_class == "commodity" and "gold" in out.agent_c.instruments[t].areas
               for t in out.selection.tickers)                                 # a gold fund (GLD / IAU / ...)


def test_eager_beginner_is_not_offered_crypto():
    out = run("eager_beginner")
    assert out.profile.product_access == "blocked_complex"
    crypto = next(o for o in out.preference_form.question("P1").options if o.id == "crypto")
    assert not crypto.available and "knowledge" in crypto.reason
    assert not any(out.agent_c.instruments[t].asset_class == "crypto" for t in out.selection.tickers)


def test_growth_investor_gets_individual_stocks_capped_and_no_energy():
    out = run("growth_investor")
    stocks = {t for t in out.selection.tickers if out.agent_c.instruments[t].product_type == "stock"}
    assert stocks and not any(out.agent_c.instruments[t].sector == "energy" for t in out.selection.tickers)
    assert all(out.result.final.weights.get(t, 0.0) <= 0.10 + 1e-6 for t in stocks)


def test_every_universe_stays_within_the_budget():
    """Agent C's universe caps (CLAUDE.md open decision 17): 100 in total, 50 single stocks, 30 crypto-assets,
    25 per interest area (cluster)."""
    for key in PERSONAS:
        out = run(key)
        ins, tickers = out.agent_c.instruments, out.selection.tickers
        assert len(tickers) <= DEFAULT_MAX_INSTRUMENTS, key
        assert sum(ins[t].product_type == "stock" for t in tickers) <= DEFAULT_MAX_SINGLE_STOCKS, key
        assert sum(ins[t].asset_class == "crypto" for t in tickers) <= DEFAULT_MAX_CRYPTO, key
        # Each area's queue serves at most 25; core funds come on top, and a fund served by one area may also
        # carry another area's tag, so the observable bound is on the non-core instruments as a whole.
        non_core = [i for i in out.selection.instruments if "core" not in i.reasons]
        assert len(non_core) <= DEFAULT_MAX_PER_AREA * len(out.selection.interest_areas), key


def test_crypto_trader_sees_the_whole_binance_market():
    out = run("crypto_trader")
    with skip_on_rate_limit():
        market = out.agent_c.get_crypto_market(AS_OF)
    assert market.n_pairs_listed > 200 and len(market.entries) > 200            # hundreds of real coins
    assert market.entries[0].ticker == "BTCUSDT" or market.entries[0].volume_rank == 1
    tickers = {e.ticker for e in market.entries}
    assert not any(t.endswith("BUSDT") and t[:-4] not in {"BNB", "ARB", "BB", "CKB", "DGB", "SHIB", "TRB", "YB"}
                   for t in tickers)                                            # no tokenized shares
    crypto_in_universe = [t for t in out.selection.tickers if out.agent_c.instruments[t].asset_class == "crypto"]
    assert 3 <= len(crypto_in_universe) <= DEFAULT_MAX_CRYPTO


def test_every_catalog_instrument_has_real_prices():
    """Guards against a mistyped or delisted ticker in Agent C's fixed catalog (one request per ticker,
    cached; with Tiingo's 50 requests an hour a first run may skip partway and continue later)."""
    source = build_live_source({"us_listed", "crypto_spot"})
    start = AS_OF - timedelta(days=30)
    missing = []
    with skip_on_rate_limit():
        for inst in DEFAULT_CATALOG:
            frame = source.fetch_adjusted_close([inst.ticker], start, AS_OF)
            if inst.ticker not in frame.columns or frame[inst.ticker].dropna().empty:
                missing.append(inst.ticker)
    assert not missing, missing
