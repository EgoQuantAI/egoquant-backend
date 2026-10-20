"""Robustness of Agent B on the paths the web flow uses: IBKR history + Risk Game + Crash Test.

Odd accounts (thin history, one trade, only deposits, all sells, crypto-only, missing prices, same-day trades,
weekend and future dates) plus seeded random mixes go through parse -> replay -> Agent B. Properties:

  * the parser and the replay raise nothing but IbkrReportError;
  * Agent B never raises, and its report is deterministic;
  * Agent B only tightens: sources/types as expected, max_stress_loss never above the class's own limit;
  * game-only evidence (game, loss acceptance, crash test) is never hard and never more than one class
    below the questionnaire class;
  * the same data cannot make a loss limit LOOSER when a stricter signal is added (monotone).

All reports and prices here are INVENTED.
"""

import random
from datetime import date

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.behavioral import BehavioralTwinAgent
from egoquant.agents.behavioral.ibkr import CASH_ID, IbkrReportError, parse_ibkr_report, to_trading_history
from egoquant.agents.behavioral.models import BehavioralData, CrashTestResult, LossAcceptance
from egoquant.demo.synthetic_investors import InMemoryBehavioralDataProvider, build_synthetic_data, eckel_grossman
from egoquant.models import DEFAULT_RISK_CLASS_POLICIES
from tests.agent_b.test_behavioral import AS_OF, run_pair, with_data
from tests.agent_b.test_ibkr import CLASSES, TODAY, deposit, flex_xml, market_prices, trade

PLAYED = "2026-09-20T10:00:00"
CLASS_ORDER = ["C1", "C2", "C3", "C4", "C5"]
LIMIT = {k: p.max_stress_loss for k, p in DEFAULT_RISK_CLASS_POLICIES.items()}
ALLOWED_TYPES = {"max_stress_loss", "max_turnover"}
ALL_CLASSES = {**CLASSES, "BTC": "crypto"}


# --------------------------------------------------------------------------
# Fixtures: one real Agent A menu per questionnaire class, built once
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def menus():
    synthetic = build_synthetic_data(seed=11)
    out = {}
    for c in CLASS_ORDER:
        result, _ = run_pair(with_data(synthetic["newcomer"], game_result=None), "newcomer", risk_class=c)
        out[c] = result.menu
    return out


def analyse(menu, data: BehavioralData):
    agent = BehavioralTwinAgent(InMemoryBehavioralDataProvider({data.investor_id: data}))
    return agent.analyze(data.investor_id, menu)


# --------------------------------------------------------------------------
# Odd accounts
# --------------------------------------------------------------------------
def many_same_day(n=30):
    return "".join(trade(day="20250303", side="BUY" if i % 2 == 0 else "SELL", qty=str(1 + i % 3)) for i in range(n))


ACCOUNTS = {
    "one_trade": flex_xml(trade()),
    "one_trade_and_deposit": flex_xml(trade(), deposit()),
    "thin_five_days": flex_xml(trade() + trade(day="20250106", side="SELL", qty="4"), deposit()),
    "only_deposits": flex_xml("", deposit() + deposit(day="20250601")),
    "only_a_withdrawal": flex_xml("", deposit(amount="-500")),
    "all_sells": flex_xml(trade(side="SELL", qty="5") + trade(day="20250210", side="SELL", qty="5")),
    "sell_more_than_bought": flex_xml(trade(qty="5") + trade(day="20250210", side="SELL", qty="50")),
    "crypto_only": flex_xml(trade(symbol="BTC", cat="CRYPTO", price="40000", qty="0.5", day="20250106")
                            + trade(symbol="BTC", cat="CRYPTO", price="30000", qty="0.5", side="SELL",
                                    day="20250610"), deposit(amount="30000")),
    "same_day_round_trip": flex_xml(trade(day="20250303") + trade(day="20250303", side="SELL"), deposit()),
    "same_day_many": flex_xml(many_same_day(), deposit()),
    "weekend_only": flex_xml(trade(day="20250104") + trade(day="20250105", side="SELL", qty="3"), deposit()),
    "future_only": flex_xml(trade(day="20261101"), deposit(day="20261101")),
    "unpriced_symbol": flex_xml(trade(symbol="XYZ", price="12") + trade(symbol="XYZ", price="9", side="SELL",
                                                                       day="20250301"), deposit()),
    "tiny_and_huge": flex_xml(trade(qty="0.000001", price="0.01") + trade(day="20250301", qty="1000000",
                                                                         price="99999")),
    "bought_then_sold_out": flex_xml(trade(qty="10") + trade(day="20250401", qty="10", side="SELL"), deposit()),
    "two_falls_two_sales": flex_xml(trade(qty="40", day="20250103") + trade(qty="30", day="20250421", side="SELL")
                                    + trade(qty="20", day="20251202", side="SELL"), deposit(amount="20000")),
}


def price_variants():
    days = pd.bdate_range("2025-01-02", "2026-09-30")
    full = market_prices()
    nan_gaps = full.copy()
    nan_gaps.iloc[::3, 0] = np.nan
    nan_gaps.iloc[10:40, 1] = np.nan
    return {
        "full": full, "no_prices": pd.DataFrame(), "only_vti": full[["VTI"]],
        "nan_gaps": nan_gaps, "all_nan": pd.DataFrame(np.nan, index=days, columns=["VTI", "AGG"]),
        "zeros": pd.DataFrame(0.0, index=days, columns=["VTI", "AGG"]),
        "unrelated_columns": pd.DataFrame({"ZZZ": np.linspace(1, 2, len(days))}, index=days),
    }


PRICES = price_variants()


def extras():
    la = lambda score, rounds=3: LossAcceptance(score=score, rounds_after_loss=rounds, timestamp=PLAYED)  # noqa: E731
    ct = lambda sold, frac, deep: CrashTestResult(sold_at_drawdown=sold, sold_fraction=frac,  # noqa: E731
                                                  max_drawdown_seen=deep, timestamp=PLAYED)
    return {
        "nothing": {},
        "cautious_game": {"game_result": eckel_grossman(1)},
        "aggressive_game": {"game_result": eckel_grossman(6)},
        "scared_loss_acceptance": {"loss_acceptance": la(5)},
        "no_losing_rounds": {"loss_acceptance": la(5, rounds=0)},
        "early_crash_sale": {"crash_test": ct(0.03, 1.0, 0.35)},
        "held_through": {"crash_test": ct(None, 0.0, 0.35)},
        "everything_cautious": {"game_result": eckel_grossman(1), "loss_acceptance": la(0),
                                "crash_test": ct(0.02, 1.0, 0.35)},
    }


EXTRAS = extras()


def build_data(xml: str, prices: pd.DataFrame, extra: dict, investor_id="fuzz") -> BehavioralData | None:
    """parse -> replay -> BehavioralData. None when the account is refused with IbkrReportError (allowed)."""
    try:
        report = parse_ibkr_report(xml, today=TODAY)
        h = to_trading_history(report, prices=prices, asset_classes=ALL_CLASSES, as_of=AS_OF)
    except IbkrReportError:
        return None
    return BehavioralData(investor_id=investor_id, history=h.history, data_source="real", **extra)


# --------------------------------------------------------------------------
# The properties
# --------------------------------------------------------------------------
def safest_loss(menu) -> float:
    return min(max(0.0, -float(p.stress["equity_down_30_bond_down_5"])) for p in menu.portfolios)


def check_report(report, menu, *, label: str):
    cls = menu.risk_class
    level = CLASS_ORDER.index(cls) + 1
    floor = LIMIT[CLASS_ORDER[max(level - 1, 1) - 1]]          # one class down, at most
    assert report.status in {"ok", "insufficient_data", "invalid_data"}, label
    for c in report.constraints:
        assert c.source == "behavioral" and c.type in ALLOWED_TYPES, f"{label}: {c.type}/{c.source}"
        assert np.isfinite(c.value) and c.value >= 0, label
        if c.type == "max_stress_loss":
            if "behavioral_limit_clamped_to_menu" in report.flags:
                # By design: when even the safest menu portfolio loses more than the evidence value, the limit
                # is raised to what the menu can deliver (so Agent A does not drop every soft limit). It is
                # then never above that safest loss; the class's own limit stays a hard constraint in Agent A.
                assert c.value <= safest_loss(menu) + 1e-3, f"{label}: clamped limit above the menu's safest"
            else:
                assert c.value <= LIMIT[cls] + 1e-9, f"{label}: loss limit {c.value} above {cls}'s {LIMIT[cls]}"
        if c.type == "max_turnover":
            assert c.value <= 1.0 + 1e-9, label
    if report.threshold_source == "game":                       # game-only evidence: soft, one class at most
        for c in report.constraints:
            if c.type == "max_stress_loss":
                assert not c.hard, f"{label}: a game-only limit must never be hard"
                assert c.value >= floor - 1e-6, f"{label}: {c.value} is more than one class below {cls}"


@pytest.mark.parametrize("account", sorted(ACCOUNTS))
@pytest.mark.parametrize("prices", sorted(PRICES))
def test_odd_accounts_never_crash_and_only_tighten(menus, account, prices):
    for extra_name, extra in EXTRAS.items():
        data = build_data(ACCOUNTS[account], PRICES[prices], extra)
        if data is None:
            continue
        for cls in ("C1", "C3", "C5"):
            label = f"{account}/{prices}/{extra_name}/{cls}"
            report = analyse(menus[cls], data)
            check_report(report, menus[cls], label=label)
            assert analyse(menus[cls], data).model_dump() == report.model_dump(), f"{label}: not deterministic"


@pytest.mark.parametrize("extra_name", sorted(EXTRAS))
@pytest.mark.parametrize("cls", CLASS_ORDER)
def test_game_only_evidence_with_no_history(menus, extra_name, cls):
    data = BehavioralData(investor_id="g", data_source="real", **EXTRAS[extra_name])
    report = analyse(menus[cls], data)
    check_report(report, menus[cls], label=f"{extra_name}/{cls}")
    assert report.threshold_source in ("game", None) or report.constraints == []


def test_everything_cautious_is_capped_one_class_for_every_class(menus):
    for cls in CLASS_ORDER:
        report = analyse(menus[cls], BehavioralData(investor_id="g", data_source="real",
                                                    **EXTRAS["everything_cautious"]))
        limits = [c for c in report.constraints if c.type == "max_stress_loss"]
        level = CLASS_ORDER.index(cls) + 1
        if not limits:
            assert cls == "C1"                                   # nothing below C1
            continue
        assert len(limits) == 1 and not limits[0].hard
        assert limits[0].value >= LIMIT[CLASS_ORDER[max(level - 1, 1) - 1]] - 1e-6
        if level > 1:
            assert "game_limit_capped_one_class" in report.flags or limits[0].value > LIMIT[CLASS_ORDER[level - 2]]


def test_a_stricter_signal_never_loosens_the_limit(menus):
    """Adding a more cautious game signal can only keep or lower the loss limit."""
    for cls in ("C3", "C4", "C5"):
        mild = analyse(menus[cls], BehavioralData(investor_id="g", data_source="real",
                                                  crash_test=EXTRAS["early_crash_sale"]["crash_test"]))
        both = analyse(menus[cls], BehavioralData(investor_id="g", data_source="real",
                                                  **EXTRAS["everything_cautious"]))
        value = lambda r: min([c.value for c in r.constraints if c.type == "max_stress_loss"] or [LIMIT[cls]])  # noqa: E731
        assert value(both) <= value(mild) + 1e-9


# --------------------------------------------------------------------------
# Seeded random accounts (no hypothesis installed): parser, replay and Agent B together
# --------------------------------------------------------------------------
SYMBOLS = [("VTI", "STK"), ("AGG", "STK"), ("BTC", "CRYPTO"), ("XYZ", "STK"), ("OPT1", "OPT"), ("EUR", "CASH")]


def random_account(rng: random.Random) -> str:
    days = pd.bdate_range("2024-12-02", "2026-12-31")
    trades = []
    for _ in range(rng.choice([0, 1, 1, 2, 3, 5, 12, 40])):
        sym, cat = rng.choice(SYMBOLS)
        day = rng.choice(days)
        if rng.random() < 0.2:
            day = day + pd.Timedelta(days=rng.choice([1, 2]))       # weekends
        trades.append(trade(day=day.strftime("%Y%m%d"), symbol=sym, cat=cat, side=rng.choice(["BUY", "SELL"]),
                            qty=str(rng.choice([0.5, 1, 5, 40, 1000, 1e-6])),
                            price=str(rng.choice([0.01, 12, 100, 250, 40000])),
                            ccy=rng.choice(["USD", "USD", "USD", "EUR"])))
    flows = [deposit(day=rng.choice(days).strftime("%Y%m%d"), amount=str(rng.choice([-5000, -1, 100, 10000, 5e6])))
             for _ in range(rng.choice([0, 1, 2, 6]))]
    return flex_xml("".join(trades), "".join(flows))


@pytest.mark.parametrize("seed", range(6))
def test_random_accounts(menus, seed):
    rng = random.Random(seed)
    names, keys = sorted(EXTRAS), sorted(PRICES)
    ran = 0
    for _ in range(25):
        xml = random_account(rng)
        data = build_data(xml, PRICES[rng.choice(keys)], EXTRAS[rng.choice(names)])
        if data is None:
            continue
        ran += 1
        cls = rng.choice(CLASS_ORDER)
        check_report(analyse(menus[cls], data), menus[cls], label=f"seed{seed}/{cls}")
    assert ran >= 1


@pytest.mark.parametrize("seed", range(3))
def test_random_garbage_only_raises_ibkr_report_error(seed):
    rng = random.Random(1000 + seed)
    for _ in range(40):
        xml = random_account(rng)
        cut = rng.randrange(0, len(xml) + 1)
        mutated = rng.choice([xml[:cut], xml.replace("tradeDate", "tradeDat", 1), xml.replace('"', "'", 7),
                              xml + "<x>", xml.replace("quantity=", "quantity=\u0000", 1), ""])
        try:
            report = parse_ibkr_report(mutated, today=TODAY)
            to_trading_history(report, prices=PRICES["nan_gaps"], asset_classes=ALL_CLASSES, as_of=AS_OF)
        except IbkrReportError:
            pass


# --------------------------------------------------------------------------
# End to end with the real Agent A: odd accounts do not break the optimizer
# --------------------------------------------------------------------------
@pytest.mark.parametrize("account", ["one_trade", "same_day_many", "crypto_only", "two_falls_two_sales",
                                     "sell_more_than_bought"])
def test_agent_a_still_meets_what_b_sends(account):
    data = build_data(ACCOUNTS[account], PRICES["full"], EXTRAS["everything_cautious"], investor_id="e2e")
    assert data is not None
    result, report = run_pair({"e2e": data}, "e2e", risk_class="C4")
    assert report.status in {"ok", "insufficient_data"}
    final = result.final
    assert final.status == "optimal"
    assert final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES["C4"].max_equity + 1e-6
    for c in report.constraints:
        if c.type == "max_stress_loss":
            assert -final.stress["equity_down_30_bond_down_5"] <= c.value + 1e-6 or final.relaxed_constraints


def test_replay_keeps_cash_nonnegative_and_weights_normalised():
    for name, xml in ACCOUNTS.items():
        for pname in ("full", "nan_gaps", "no_prices"):
            try:
                h = to_trading_history(parse_ibkr_report(xml, today=TODAY), prices=PRICES[pname],
                                       asset_classes=ALL_CLASSES, as_of=AS_OF)
            except IbkrReportError:
                continue
            assert (h.history.positions[CASH_ID] >= -1e-9).all(), name
            assert np.isfinite(h.history.positions.to_numpy()).all(), f"{name}/{pname}"
            if h.final_weights:
                assert sum(h.final_weights.values()) == pytest.approx(1.0)


def test_evidence_no_stricter_than_the_class_emits_no_limit(menus):
    """Sales at a 5.7% fall while the class (C1) already allows only 5%: nothing to tighten, so no looser limit."""
    data = build_data(ACCOUNTS["two_falls_two_sales"], PRICES["full"], {})
    report = analyse(menus["C1"], data)
    assert report.capitulation_threshold is not None and report.capitulation_threshold > LIMIT["C1"]
    assert "behavioral_limit_not_below_class" in report.flags
    assert [c for c in report.constraints if c.type == "max_stress_loss"] == []


# --------------------------------------------------------------------------
# No echo: an error (or a successful parse) never carries a value from the file
# --------------------------------------------------------------------------
MARK = "ZQ$7MARK"


@pytest.mark.parametrize("field", ["symbol", "currency", "tradeDate", "quantity", "tradePrice", "buySell",
                                   "assetCategory"])
def test_errors_never_echo_a_value_from_the_file(field):
    attrs = dict(currency="USD", assetCategory="STK", symbol="VTI", tradeDate="20250103", quantity="10",
                 tradePrice="250", buySell="BUY", levelOfDetail="EXECUTION")
    attrs[field] = MARK
    row = "<Trade " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + " />"
    for xml in (flex_xml(row), flex_xml(row + trade())):
        try:
            report = parse_ibkr_report(xml, today=TODAY)
        except IbkrReportError as error:
            assert MARK not in str(error) and "MARK" not in str(error)
        else:                                     # accepted or skipped: still nothing from the file in the output
            assert MARK not in report.model_dump_json()


def test_unknown_columns_are_dropped_not_kept():
    row = ('<Trade currency="USD" assetCategory="STK" symbol="VTI" tradeDate="20250103" quantity="10" '
           f'tradePrice="250" buySell="BUY" levelOfDetail="EXECUTION" accountId="U9999999" description="{MARK}" />')
    report = parse_ibkr_report(flex_xml(row, deposit()), today=TODAY)
    h = to_trading_history(report, prices=PRICES["full"], asset_classes=ALL_CLASSES, as_of=AS_OF)
    dumped = (report.model_dump_json() + repr(h.notes) + repr(h.final_weights) + repr(list(h.history.positions.columns))
              + repr(h.history.trades) + repr(h.history.asset_classes))
    assert MARK not in dumped and "U9999999" not in dumped


# --------------------------------------------------------------------------
# The single-stock universe (Agent C now offers up to 25 single stocks): odd accounts through the whole pipeline
# --------------------------------------------------------------------------
@pytest.mark.parametrize("persona, account", [("growth_investor", "two_falls_two_sales"),
                                              ("growth_investor", "thin_five_days"),
                                              ("crypto_trader", "two_falls_two_sales"),
                                              ("crypto_trader", "crypto_only")])
def test_odd_accounts_with_the_single_stock_universe(persona, account):
    from egoquant.agents.market_intel import SyntheticPriceSource
    from egoquant.demo.personas import PERSONAS, run_persona
    p = PERSONAS[persona]
    data = build_data(ACCOUNTS[account], PRICES["full"], EXTRAS["everything_cautious"], investor_id=p.investor_id)
    assert data is not None
    agent = BehavioralTwinAgent(InMemoryBehavioralDataProvider({p.investor_id: data}))
    out = run_persona(p, AS_OF, price_source=SyntheticPriceSource(), behavioral_agent=agent)

    instruments = out.agent_c.instruments
    stocks = [t for t in out.selection.tickers if instruments[t].product_type == "stock"]
    assert stocks, "the persona asked for individual stocks, so the universe must hold some"
    cls = out.profile.c_level
    behavioral = [c for c in out.review_constraints if c.source == "behavioral"]
    for c in behavioral:                                       # tighten only, whatever the universe
        assert c.type in ALLOWED_TYPES and np.isfinite(c.value) and c.value >= 0
        if c.type == "max_stress_loss":
            assert c.value <= LIMIT[cls] + 1e-9 or out.result.menu is not None and c.value <= safest_loss(
                out.result.menu) + 1e-3
    assert out.review is not None and out.result.final is not None
    if out.approved:
        assert out.result.final.equity_weight <= DEFAULT_RISK_CLASS_POLICIES[cls].max_equity + 1e-6
        assert all(w <= 0.10 + 1e-6 for t, w in out.result.final.weights.items()
                   if instruments[t].product_type == "stock")        # 10% cap per single stock (PROVISIONAL)
