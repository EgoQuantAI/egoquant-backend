"""Trade check: property sweep over random planned trades on several personas (Damla's Z5-lite).

Properties, for every random trade on every persona:
  * deterministic: the same input twice gives the same answer; the order of the holdings does not matter
  * never "advisable" when the after-trade portfolio breaks a hard limit (checked by an independent
    RiskEngine run with the limits the pipeline itself used for that persona)
  * selling never raises the stock share
  * a buy of a product the investor may not hold (or that is not in our list) is never "advisable"
  * odd amounts (tiny, huge, the whole account, the whole holding) never raise anything but TradeCheckError
  * findings are not repeated and are sorted blocking, warning, info
  * `direction` never contradicts the numbers: it is recomputed here from the before/after figures
  * single stocks (R4, 10% cap): selling a whole holding, buying above the cap, a stock plus a cash-only account

SYNTHETIC prices only; no network.
"""

from __future__ import annotations

import os
import random
from datetime import date

import pytest

from egoquant.agents.market_intel import DEFAULT_CATALOG, SyntheticPriceSource
from egoquant.demo.personas import PERSONAS, behavioral_agent_for
from egoquant.models import PortfolioProposal
from egoquant.pipeline import run_pipeline
from egoquant.risk_engine import RiskEngine
from egoquant.trade_check import CASH_ID, PlannedTrade, TradeCheckError, check_trade
from tests.helpers import CONFIG

AS_OF = date(2026, 9, 30)
PERSONA_KEYS = ["no_loss_saver", "cautious_retiree", "balanced_saver", "growth_investor", "crypto_trader"]
TRADES_PER_PERSONA = int(os.environ.get("TRADE_SWEEP_N", "80"))   # a longer run: TRADE_SWEEP_N=1000
_RUNS: dict = {}


def _run(key):
    if key not in _RUNS:
        p = PERSONAS[key]
        out = run_pipeline(p.answers(), investor_id=p.investor_id, interest_areas=None,
                           preferences=p.preference_answers(), goal_type=p.goal_type, horizon_years=p.horizon_years,
                           as_of=AS_OF, trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p),
                           price_source=SyntheticPriceSource(), min_cash_buffer=p.min_cash_buffer,
                           liquidity_need=p.liquidity_need)
        assets = out.agent_c.get_market_snapshot(out.agent_c.selection.tickers, AS_OF, 504).assets
        _RUNS[key] = (out, assets)
    return _RUNS[key]


def _call(key, holdings, ticker, side, amount):
    out, assets = _run(key)
    p = PERSONAS[key]
    return check_trade(holdings=holdings, planned=PlannedTrade(ticker=ticker, side=side, amount=amount),
                       profile=out.profile, profile_config=CONFIG, selection=out.selection, assets=assets,
                       behavioral_constraints=out.result.behavioral_constraints, as_of=AS_OF,
                       min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)


def _random_case(rng: random.Random, key: str):
    out, _ = _run(key)
    pool = sorted({*out.selection.tickers, *rng.sample([i.ticker for i in DEFAULT_CATALOG], 25)})
    held = rng.sample(pool, rng.randint(0, 5))
    holdings = {t: rng.choice([50.0, 1_000.0, 20_000.0, 90_000.0]) * rng.random() + 1.0 for t in held}
    if rng.random() < 0.8:
        holdings[CASH_ID] = rng.choice([0.0, 100.0, 5_000.0, 60_000.0]) * rng.random()
    holdings = {t: v for t, v in holdings.items() if v > 0}
    total = sum(holdings.values())
    if rng.random() < 0.7 and holdings and any(t != CASH_ID for t in holdings):
        ticker = rng.choice([t for t in holdings if t != CASH_ID])
        side = rng.choice(["buy", "sell"])
    else:
        ticker, side = rng.choice(pool + ["ZZZZ"]), "buy"
    amount = rng.choice([0.01, 1.0, 100.0, 5_000.0, 50_000.0, max(total, 1.0)]) * rng.uniform(0.2, 1.0)
    if side == "sell":
        amount = min(amount, holdings[ticker])
    return holdings, ticker, side, amount


def _approved_independently(key, check) -> bool:
    out, assets = _run(key)
    infos = {a.asset_id: a for a in assets}
    from egoquant.models import AssetInfo
    for i in DEFAULT_CATALOG:
        infos.setdefault(i.ticker, AssetInfo(asset_id=i.ticker, asset_class=i.asset_class, duration_years=i.duration_years,
                                             convexity=i.convexity, max_weight=i.max_weight))
    infos.setdefault(CASH_ID, AssetInfo(asset_id=CASH_ID, asset_class="cash"))
    engine = RiskEngine(catalog=out.agent_c.catalog, scenarios=None)
    engine.r_levels.setdefault(CASH_ID, 1)
    proposal = PortfolioProposal(stage="constrained", method="sweep", as_of=AS_OF, status="optimal",
                                 weights=dict(check.after.weights))
    # The risk-class cash cap is Agent A's construction rule (DESIGN.md decision 14); the trade check does not
    # apply it to the investor's own trades (trade_check._limits, branch trade-check-cash), so this independent
    # check skips it too. Every other limit the pipeline's Risk Engine used still applies.
    constraints = [c for c in out.review_constraints
                   if not (c.type == "max_class_weight" and c.asset_class == "cash" and c.source == "risk_class")]
    return engine.review(proposal, list(infos.values()), constraints, out.profile.max_r_level).approved



DIRECTION_STEP = 0.005          # the trade check's own threshold for "this measure moved"
STOCKS = {i.ticker for i in DEFAULT_CATALOG if i.product_type == "stock"}


def _largest_non_cash(n):
    return max([w for t, w in n.weights.items() if t != CASH_ID], default=0.0)


def _expected_direction(c):
    """The direction recomputed from the published before/after numbers only (not from trade_check's internals)."""
    if c.before is None:
        return "no_change", []
    moves = []
    d = c.after.equity_share - c.before.equity_share
    if abs(d) > DIRECTION_STEP:
        moves.append(("stock share", d))
    if c.before.stress_loss is not None and c.after.stress_loss is not None:
        d = c.after.stress_loss - c.before.stress_loss
        if abs(d) > DIRECTION_STEP:
            moves.append(("loss", d))
    d = _largest_non_cash(c.after) - _largest_non_cash(c.before)
    if abs(d) > DIRECTION_STEP:
        moves.append(("largest", d))
    if not moves:
        return "no_change", []
    if all(m < 0 for _, m in moves):
        return "lowers_exposure", moves
    if all(m > 0 for _, m in moves):
        return "raises_exposure", moves
    return "mixed", moves


def _near_a_step(c) -> bool:
    """A measure sitting within rounding of the 0.5-point step may be called either way; skip those."""
    if c.before is None:
        return False
    gaps = [abs(c.after.equity_share - c.before.equity_share),
            abs(_largest_non_cash(c.after) - _largest_non_cash(c.before))]
    if c.before.stress_loss is not None and c.after.stress_loss is not None:
        gaps.append(abs(c.after.stress_loss - c.before.stress_loss))
    return any(abs(g - DIRECTION_STEP) < 1e-5 for g in gaps)


def _check_direction(c, ctx):
    if _near_a_step(c):
        return
    direction, moves = _expected_direction(c)
    assert c.direction == direction, (ctx, c.direction, direction, moves)
    assert len(c.direction_parts) == len(moves), (ctx, c.direction_parts, moves)
    for part, (_, move) in zip(c.direction_parts, moves):                  # the wording agrees with the sign
        assert (" falls " in part) == (move < 0) and (" rises " in part) == (move > 0), (ctx, part, move)


def _check_invariants(key, c, ctx):
    """What 'advisable' means, plus the direction, for one checked trade."""
    if c.verdict == "advisable":
        assert c.risk_review.approved and _approved_independently(key, c), ctx
        assert not any(r.severity in ("blocking", "warning") for r in c.reasons), ctx
    if c.verdict == "not_advisable":
        assert c.blocking, ctx
    expected = ("not_advisable" if c.blocking else
                "allowed_with_warnings" if any(r.severity == "warning" for r in c.reasons) else "advisable")
    assert c.verdict == expected, ctx
    _check_direction(c, ctx)


@pytest.mark.parametrize("key", PERSONA_KEYS)
def test_random_trades_keep_the_properties(key):
    out, _ = _run(key)
    rng = random.Random(f"sweep-{key}")
    not_allowed = set(out.selection.assets_to_exclude) | {e.ticker for e in out.selection.excluded}
    r_levels = {i.ticker: i.r_level for i in DEFAULT_CATALOG}
    done = 0
    for _ in range(TRADES_PER_PERSONA):
        holdings, ticker, side, amount = _random_case(rng, key)
        try:
            c = _call(key, holdings, ticker, side, amount)
        except TradeCheckError:
            continue                                   # e.g. selling something not held: refused, never a crash
        done += 1
        ctx = (key, holdings, ticker, side, amount)

        # deterministic, and independent of the order of the holdings
        again = _call(key, dict(reversed(list(holdings.items()))), ticker, side, amount)
        assert c.verdict == again.verdict, ctx
        assert [(r.code, r.severity, r.ticker) for r in c.reasons] == [(r.code, r.severity, r.ticker)
                                                                       for r in again.reasons], ctx
        assert _call(key, holdings, ticker, side, amount).model_dump() == c.model_dump(), ctx

        # advisable means the independent Risk Engine run agrees
        if c.verdict == "advisable":
            assert c.risk_review.approved and _approved_independently(key, c), ctx
            assert not any(r.severity in ("blocking", "warning") for r in c.reasons), ctx
        if c.verdict == "not_advisable":
            assert c.blocking, ctx
        if not c.risk_review.approved and not _only_reduces(c):
            assert c.verdict != "advisable", ctx

        _check_direction(c, ctx)

        # selling never raises the stock share
        if side == "sell" and c.before is not None:
            assert c.after.equity_share <= c.before.equity_share + 1e-9, ctx

        # a product the investor may not hold, or we do not know, is never advisable to buy
        if side == "buy":
            refused = (ticker in not_allowed or ticker not in r_levels
                       or r_levels[ticker] > out.profile.max_r_level)
            if refused:
                assert c.verdict == "not_advisable", ctx

        # structure: no repeated finding, blocking first
        keys = [(r.code, r.ticker, r.scenario, r.source, r.message) for r in c.reasons]
        assert len(keys) == len(set(keys)), ctx
        codes = [(r.code, r.ticker, r.scenario, r.limit) for r in c.reasons if r.severity == "blocking"]
        assert len(codes) == len(set(codes)), ctx
        order = [{"blocking": 0, "warning": 1, "info": 2}[r.severity] for r in c.reasons]
        assert order == sorted(order), ctx
        if c.after.total_value > 0:                    # all of it can be unknown products: nothing left to measure
            assert abs(sum(c.after.weights.values()) - 1.0) < 1e-6, ctx
        else:
            assert c.verdict != "advisable", ctx
    assert done >= TRADES_PER_PERSONA // 2, "the generator refused too many cases to be a useful sweep"


def _only_reduces(check) -> bool:
    """The Risk Engine may still reject an existing breach that this trade did not create or worsen."""
    return any(r.code == "reduces_breach" for r in check.reasons)


@pytest.mark.parametrize("key", PERSONA_KEYS)
def test_odd_amounts_never_crash(key):
    out, _ = _run(key)
    safe = next(t for t in out.selection.tickers if t != CASH_ID)
    base = {CASH_ID: 1_000.0, safe: 4_000.0}
    total = sum(base.values())
    cases = [(safe, "buy", 1e-12), (safe, "buy", 1e-6), (safe, "buy", 0.01), (safe, "buy", 1e12),
             (safe, "buy", total), (safe, "buy", 1_000.0), (safe, "sell", 4_000.0), (safe, "sell", 1e-9),
             (safe, "sell", 4_000.0 * (1 - 1e-12))]
    for ticker, side, amount in cases:
        c = _call(key, base, ticker, side, amount)
        assert c.verdict in ("advisable", "allowed_with_warnings", "not_advisable")
        assert abs(sum(c.after.weights.values()) - 1.0) < 1e-6
    for ticker, side, amount in [(safe, "sell", 4_000.01 * 1e6), (safe, "sell", 4_001.0), ("NOPE", "sell", 1.0),
                                 (CASH_ID, "buy", 10.0)]:
        with pytest.raises(TradeCheckError):
            _call(key, base, ticker, side, amount)
    with pytest.raises(TradeCheckError):                      # holdings that are not money
        _call(key, {safe: float("nan")}, safe, "buy", 10.0)
    with pytest.raises(TradeCheckError):
        _call(key, {safe: -5.0}, safe, "buy", 10.0)
    c = _call(key, {}, safe, "buy", 1_000.0)                   # an empty account: everything is new money
    assert c.before is None and any(r.code == "new_money" for r in c.reasons)


# ==========================================================================
# Single stocks: R4, capped at 10% of the account (Agent C's catalog)
# ==========================================================================
CAP = 0.10
STOCK_PERSONAS = ["growth_investor", "crypto_trader"]            # may hold R4 and above


def _parts(key):
    out, _ = _run(key)
    universe = [t for t in out.selection.tickers if t != CASH_ID]
    stocks = [t for t in universe if t in STOCKS] or ["AAPL", "MSFT", "JNJ"]       # the catalog's, if the persona has none
    classes = {i.ticker: i.asset_class for i in DEFAULT_CATALOG}
    bonds = [t for t in universe if classes.get(t) == "bond"] or ["AGG", "BND"]
    return stocks[0], stocks[1], bonds[0], (bonds[1] if len(bonds) > 1 else "BND")


@pytest.mark.parametrize("key", STOCK_PERSONAS)
def test_selling_a_whole_single_stock_holding(key):
    stock, _, bond1, bond2 = _parts(key)
    holdings = {stock: 6_000.0, bond1: 30_000.0, bond2: 20_000.0, CASH_ID: 4_000.0}
    c = _call(key, holdings, stock, "sell", 6_000.0)               # the whole holding: no error, no dust left
    assert stock not in c.after.weights or c.after.weights[stock] < 1e-9
    assert c.after.weights[CASH_ID] == pytest.approx(10_000.0 / 60_000.0)             # proceeds stay as cash
    assert c.after.equity_share < c.before.equity_share
    assert not any(r.ticker == stock and r.severity == "blocking" for r in c.reasons)
    assert c.direction != "raises_exposure"
    _check_invariants(key, c, (key, "sell whole", stock))
    almost = _call(key, holdings, stock, "sell", 6_000.0 * (1 - 1e-12))              # a hair less: still fine
    assert almost.after.equity_share <= almost.before.equity_share
    with pytest.raises(TradeCheckError):
        _call(key, holdings, stock, "sell", 6_000.01)                                 # more than held is refused


@pytest.mark.parametrize("key", STOCK_PERSONAS)
def test_buying_a_single_stock_above_its_cap_is_blocked_at_the_cap_and_not_below(key):
    stock, _, bond1, bond2 = _parts(key)
    base = {bond1: 40_000.0, bond2: 40_000.0, CASH_ID: 20_000.0}                      # 100,000: 1,000 = 1%
    over = _call(key, base, stock, "buy", 10_500.0)                                   # 10.5% > 10%
    assert over.verdict == "not_advisable"
    cap = [r for r in over.reasons if r.code == "instrument_cap"]
    assert cap and cap[0].severity == "blocking" and cap[0].limit == pytest.approx(CAP)
    assert cap[0].ticker == stock and "10.5%" in cap[0].message and "10.0%" in cap[0].message
    assert not over.risk_review.approved                                              # the Risk Engine agrees
    for amount in (9_000.0, 9_900.0, 10_000.0):                                        # up to the cap itself: allowed
        ok = _call(key, base, stock, "buy", amount)
        assert not any(r.code == "instrument_cap" and r.severity == "blocking" for r in ok.reasons), amount
        _check_invariants(key, ok, (key, "buy up to the cap", amount))
    near = _call(key, base, stock, "buy", 9_900.0)                                     # close to the cap: said so
    assert any(r.code == "near_limit" and r.ticker == stock for r in near.reasons) and near.verdict != "advisable"
    _check_invariants(key, over, (key, "buy over cap"))
    # buying more of a stock already above its cap is worse, never advisable; selling some of it moves closer
    held = {stock: 15_000.0, bond1: 45_000.0, CASH_ID: 40_000.0}                      # 15% now
    worse = _call(key, held, stock, "buy", 1_000.0)
    assert worse.verdict == "not_advisable" and any(r.code == "instrument_cap" for r in worse.reasons)
    closer = _call(key, held, stock, "sell", 2_000.0)                                  # 15% -> 13%
    assert any(r.code == "reduces_breach" and r.ticker == stock for r in closer.reasons)
    assert not any(r.code == "instrument_cap" and r.severity == "blocking" for r in closer.reasons)
    for c, what in ((worse, "worse"), (closer, "closer")):
        _check_invariants(key, c, (key, what))


@pytest.mark.parametrize("key", STOCK_PERSONAS)
def test_a_single_stock_with_a_cash_only_account(key):
    stock, other, _, _ = _parts(key)
    cash_only = {CASH_ID: 20_000.0}
    first = _call(key, cash_only, stock, "buy", 2_000.0)                               # 10% of the account: at the cap
    assert not any(r.code == "instrument_cap" and r.severity == "blocking" for r in first.reasons)
    assert first.before.equity_share == 0.0 and first.after.equity_share == pytest.approx(0.10)
    assert first.direction == "raises_exposure"
    too_much = _call(key, cash_only, stock, "buy", 4_000.0)                            # 20%
    assert too_much.verdict == "not_advisable" and any(r.code == "instrument_cap" for r in too_much.reasons)
    both = {stock: 3_000.0, CASH_ID: 17_000.0}                                          # 15%: already above the cap
    for side, amount in (("sell", 1_000.0), ("sell", 3_000.0), ("buy", 500.0)):
        c = _call(key, both, stock, side, amount)
        _check_invariants(key, c, (key, side, amount))
        if side == "sell":
            assert c.after.equity_share <= c.before.equity_share
        else:
            assert c.verdict == "not_advisable"                                         # worsens an existing breach
    second = _call(key, {stock: 1_500.0, CASH_ID: 18_500.0}, other, "buy", 1_500.0)     # two stocks, 7.5% each
    _check_invariants(key, second, (key, "second stock"))
    for c, what in ((first, "first"), (too_much, "too much")):
        _check_invariants(key, c, (key, what))


def test_a_lower_risk_investor_may_sell_a_single_stock_but_never_buy_one():
    key = "balanced_saver"                                                   # highest product risk level R3: no R4 stocks
    stock, _, bond1, bond2 = _parts(key)
    holdings = {stock: 2_000.0, bond1: 30_000.0, bond2: 30_000.0, CASH_ID: 8_000.0}
    for amount in (1.0, 100.0, 5_000.0):
        buy = _call(key, {bond1: 30_000.0, CASH_ID: 20_000.0}, stock, "buy", amount)
        assert buy.verdict == "not_advisable" and any(r.code == "not_allowed" for r in buy.reasons), amount
        _check_invariants(key, buy, (key, "buy", amount))
    sale = _call(key, holdings, stock, "sell", 2_000.0)                                # the way out is never blocked
    assert not any(r.code == "not_allowed" for r in sale.reasons)
    _check_invariants(key, sale, (key, "sell whole"))


def test_the_direction_oracle_catches_a_contradiction():
    key = "growth_investor"
    stock, _, bond1, bond2 = _parts(key)
    c = _call(key, {stock: 6_000.0, bond1: 30_000.0, bond2: 20_000.0, CASH_ID: 4_000.0}, stock, "sell", 6_000.0)
    _check_direction(c, "real")                                                       # the genuine answer passes
    assert c.direction == "lowers_exposure" and c.direction_parts
    with pytest.raises(AssertionError):
        _check_direction(c.model_copy(update={"direction": "raises_exposure"}), "wrong direction")
    flipped = [p.replace(" falls ", " rises ") for p in c.direction_parts]
    with pytest.raises(AssertionError):
        _check_direction(c.model_copy(update={"direction_parts": flipped}), "wrong verb")
    with pytest.raises(AssertionError):
        _check_direction(c.model_copy(update={"direction_parts": c.direction_parts[:-1]}), "missing part")
