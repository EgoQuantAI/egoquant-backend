"""Trade check (egoquant/trade_check.py): models. Synthetic data only."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from egoquant.trade_check import PlannedTrade, TradeReason


def test_planned_trade_normalises_the_ticker():
    t = PlannedTrade(ticker=" vti ", side="buy", amount=1000)
    assert t.ticker == "VTI" and t.side == "buy" and t.amount == 1000


@pytest.mark.parametrize("bad", [dict(ticker="VTI", side="hold", amount=1), dict(ticker="VTI", side="buy", amount=0),
                                 dict(ticker="VTI", side="buy", amount=-5),
                                 dict(ticker="VTI", side="buy", amount=float("inf")),
                                 dict(ticker="VTI", side="buy", amount=float("nan")),
                                 dict(ticker="VT;DROP", side="buy", amount=1), dict(ticker="", side="buy", amount=1),
                                 dict(ticker="X" * 16, side="buy", amount=1),
                                 dict(ticker="VTI", side="buy", amount=1, note="extra fields are refused")])
def test_planned_trade_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        PlannedTrade(**bad)


def test_reason_is_plain_data():
    r = TradeReason(code="max_equity", severity="blocking", message="m", source="suitability",
                    limit=0.4, before=0.35, after=0.45)
    assert r.model_dump()["after"] == 0.45


# ==========================================================================
# check_trade: logic (synthetic pipeline run for the profile, selection and asset metadata)
# ==========================================================================
from datetime import date, datetime, timezone  # noqa: E402

from egoquant.agents.market_intel import SyntheticPriceSource  # noqa: E402
from egoquant.models import AgentConfig, Constraint  # noqa: E402
from egoquant.pipeline import run_pipeline  # noqa: E402
from egoquant.trade_check import CASH_ID, TradeCheckError, check_trade  # noqa: E402
from tests.helpers import make_answers  # noqa: E402

AS_OF = date(2026, 9, 30)
CASES = {"C1": dict(gl_score=15), "C3": dict(gl_score=25), "C5": dict(gl_score=40)}
_RUNS: dict = {}


def _run(c_level):
    if c_level not in _RUNS:
        out = run_pipeline(make_answers(**CASES[c_level]), investor_id="t", goal_type="growth", horizon_years=10,
                           as_of=AS_OF, price_source=SyntheticPriceSource(), interest_areas=["stocks", "bonds", "gold"])
        assets = out.agent_c.get_market_snapshot(out.agent_c.selection.tickers, AS_OF, 504).assets
        _RUNS[c_level] = (out, assets)
    return _RUNS[c_level]


def _check(c_level, holdings, ticker, side, amount, **kw):
    out, assets = _run(c_level)
    kw.setdefault("behavioral_constraints", [])
    return check_trade(holdings=holdings, planned=PlannedTrade(ticker=ticker, side=side, amount=amount),
                       profile=out.profile, profile_config=_config(), selection=out.selection, assets=assets,
                       as_of=AS_OF, **kw)


def _config():
    from tests.helpers import CONFIG
    return CONFIG


def test_a_small_cash_to_bond_buy_is_advisable_for_a_cautious_investor():
    c = _check("C1", {CASH_ID: 10_000, "SHY": 40_000}, "SHY", "buy", 1_000)
    assert c.verdict in ("advisable", "allowed_with_warnings")
    assert c.risk_review.approved and not c.blocking
    assert c.after.total_value == pytest.approx(50_000) and c.before.total_value == pytest.approx(50_000)


def test_buying_a_stock_fund_breaks_a_cautious_investors_equity_limit():
    c = _check("C1", {CASH_ID: 50_000}, "VT", "buy", 20_000)
    assert c.verdict == "not_advisable" and not c.risk_review.approved
    assert any(r.severity == "blocking" and r.code in ("max_equity", "r_level", "max_stress_loss") for r in c.reasons)


def test_a_buy_with_too_little_cash_counts_as_new_money():
    c = _check("C3", {CASH_ID: 1_000, "SHY": 9_000}, "SHY", "buy", 5_000)
    assert any(r.code == "new_money" for r in c.reasons)
    assert c.after.total_value == pytest.approx(14_000) and c.verdict != "advisable"


def test_selling_more_than_held_or_something_not_held_is_refused():
    with pytest.raises(TradeCheckError):
        _check("C3", {CASH_ID: 1_000, "SHY": 9_000}, "SHY", "sell", 9_500)
    with pytest.raises(TradeCheckError):
        _check("C3", {CASH_ID: 1_000, "SHY": 9_000}, "AGG", "sell", 10)
    with pytest.raises(TradeCheckError):
        _check("C3", {CASH_ID: 1_000}, CASH_ID, "buy", 10)


def test_a_sale_goes_to_cash_and_keeps_the_total():
    c = _check("C3", {CASH_ID: 1_000, "SHY": 9_000}, "SHY", "sell", 4_000)
    assert c.after.weights[CASH_ID] == pytest.approx(0.5) and c.after.total_value == pytest.approx(10_000)


def test_an_unknown_product_cannot_be_bought_but_can_be_sold():
    buy = _check("C3", {CASH_ID: 5_000}, "ZZZZ", "buy", 100)
    assert buy.verdict == "not_advisable" and buy.traded_r_level is None
    sell = _check("C3", {CASH_ID: 5_000, "ZZZZ": 1_000}, "ZZZZ", "sell", 500)
    assert sell.verdict != "not_advisable" and any(r.code == "unknown_instrument" for r in sell.reasons)


def test_holdings_outside_the_product_list_are_left_out_and_said_so():
    c = _check("C3", {CASH_ID: 5_000, "SHY": 5_000, "ZZZZ": 1_000}, "SHY", "buy", 1_000)
    assert any(r.code == "left_out" for r in c.reasons) and c.verdict == "allowed_with_warnings"


def test_a_trade_that_lowers_an_existing_breach_is_a_warning_not_a_block():
    # a C1 investor already holds too much stock; selling some of it reduces the breach
    c = _check("C1", {CASH_ID: 30_000, "VT": 20_000}, "VT", "sell", 10_000)
    assert c.verdict == "allowed_with_warnings" and any(r.code == "reduces_breach" for r in c.reasons)
    worse = _check("C1", {CASH_ID: 30_000, "VT": 20_000}, "VT", "buy", 5_000)     # stock share goes up
    assert worse.verdict == "not_advisable"


def test_weak_behavioral_limits_are_ignored_and_strong_ones_apply():
    cap = lambda conf: Constraint(type="max_class_weight", value=0.10, source="behavioral", asset_class="bond",
                                  confidence=conf)
    base = {CASH_ID: 20_000, "SHY": 5_000}
    weak = _check("C3", base, "SHY", "buy", 10_000, behavioral_constraints=[cap(0.2)])
    strong = _check("C3", base, "SHY", "buy", 10_000, behavioral_constraints=[cap(0.9)])
    assert not any(r.source == "behavioral" for r in weak.reasons)
    assert strong.verdict == "not_advisable" and any(r.source == "behavioral" for r in strong.blocking)


def test_a_soft_behavioral_limit_only_warns():
    soft = Constraint(type="max_class_weight", value=0.10, source="behavioral", asset_class="bond", hard=False)
    c = _check("C3", {CASH_ID: 20_000, "SHY": 5_000}, "SHY", "buy", 10_000, behavioral_constraints=[soft])
    assert c.verdict == "allowed_with_warnings" and any(r.code == "soft_limit" for r in c.reasons)


def test_an_expired_profile_warns():
    out, assets = _run("C3")
    late = date(2030, 1, 1)
    c = check_trade(holdings={CASH_ID: 1_000, "SHY": 9_000}, planned=PlannedTrade(ticker="SHY", side="buy", amount=100),
                    profile=out.profile, profile_config=_config(), selection=out.selection, assets=assets,
                    behavioral_constraints=[], as_of=late)
    assert any(r.code == "profile_expired" for r in c.reasons) and c.verdict != "advisable"


def test_deterministic_and_ordered_blocking_first():
    args = ({CASH_ID: 10_000, "SHY": 5_000}, "AGG", "buy", 3_000)
    a, b = _check("C3", *args), _check("C3", *args)
    assert a.model_dump() == b.model_dump()
    sev = [r.severity for r in a.reasons]
    assert sev == sorted(sev, key={"blocking": 0, "warning": 1, "info": 2}.get)


def test_never_advisable_when_the_risk_engine_rejects_the_result():
    import random
    rng = random.Random(7)
    out, assets = _run("C3")
    tickers = [a.asset_id for a in assets][:12]
    for _ in range(60):
        holdings = {t: rng.uniform(100, 20_000) for t in rng.sample(tickers, 3)}
        holdings[CASH_ID] = rng.uniform(0, 5_000)
        t = rng.choice(tickers)
        side = rng.choice(["buy", "sell"])
        amount = rng.uniform(10, 10_000)
        if side == "sell":
            held = holdings.get(t, 0)
            if held <= 0:
                continue
            amount = min(amount, held)
        c = _check("C3", holdings, t, side, amount)
        if c.verdict == "advisable":
            assert c.risk_review.approved


# ==========================================================================
# trade-check-cash: moves to cash are not blocked, direction, clearer wording
# ==========================================================================
FUNDS = {"VTI": 50_000, "AGG": 30_000, CASH_ID: 10_000, "IAU": 10_000}       # C5 cases A and B of the task


def test_selling_into_cash_is_not_blocked_by_the_builders_cash_cap():
    c = _check("C5", FUNDS, "VTI", "sell", 20_000)                 # case A: cash 10% -> 30%, builder cap 15%
    assert not any(r.code == "max_class_weight" and "cash" in r.message for r in c.reasons)
    assert c.risk_review.approved
    # advisable: VTI falls from 50% to 30%, below the 40% one-holding limit, every other measure stays inside its
    # limit, and the cash cap is a portfolio-construction rule that the check does not apply, so nothing is left
    # to block or warn about
    assert c.verdict == "advisable" and not c.reasons
    assert any("cash cap (15%)" in n for n in c.notes)


def test_a_sale_that_only_raises_cash_is_advisable():
    c = _check("C5", {"VTI": 35_000, "AGG": 35_000, "IAU": 10_000, CASH_ID: 20_000}, "VTI", "sell", 10_000)
    assert c.before.largest_weight < 0.40 and c.after.largest_weight < 0.40
    assert c.after.class_shares["cash"] == pytest.approx(0.30)        # above C5's 15% builder cap
    assert c.verdict == "advisable" and c.risk_review.approved


def test_buying_stock_above_the_equity_cap_is_still_not_advisable():
    c = _check("C3", {"VTI": 35_000, "AGG": 45_000, CASH_ID: 20_000}, "VTI", "buy", 15_000)   # stocks 35% -> 50%
    assert c.verdict == "not_advisable" and not c.risk_review.approved
    assert any(r.code == "max_equity" and r.severity == "blocking" for r in c.reasons)


def test_a_behavioral_cash_limit_still_applies():
    cap = Constraint(type="max_class_weight", value=0.15, source="behavioral", asset_class="cash", confidence=0.9)
    c = _check("C5", FUNDS, "VTI", "sell", 20_000, behavioral_constraints=[cap])
    assert c.verdict == "not_advisable" and any(r.source == "behavioral" for r in c.blocking)
    assert c.direction == "lowers_exposure"                         # the direction never changes the verdict


def test_the_investors_own_cash_buffer_warns_when_a_buy_eats_into_it():
    holdings = {CASH_ID: 12_000, "VTI": 30_000, "AGG": 39_000, "IAU": 19_000}
    c = _check("C5", holdings, "VTI", "buy", 7_000, min_cash_buffer=0.10)      # cash 12% -> 5%
    assert any(r.code == "min_cash" and r.severity == "warning" for r in c.reasons) and c.verdict != "advisable"
    assert not any(r.code == "min_cash" for r in _check("C5", holdings, "VTI", "buy", 7_000).reasons)


def test_direction_of_a_move_to_cash_lowers_all_three_measures():
    c = _check("C5", FUNDS, "VTI", "sell", 20_000)
    assert c.direction == "lowers_exposure" and len(c.direction_parts) == 3
    assert c.direction_parts[0] == "Your stock share falls from 50.0% to 30.0%."
    assert "'Severe crash (stocks -30%)'" in c.direction_parts[1]
    assert "equity_down_30_bond_down_5" not in " ".join(c.direction_parts)


def test_direction_of_buying_stock_with_cash_raises_exposure():
    c = _check("C5", {"VTI": 30_000, "AGG": 30_000, CASH_ID: 40_000}, "VTI", "buy", 5_000)
    assert c.direction == "raises_exposure" and all(" rises " in p for p in c.direction_parts)


def test_a_tiny_trade_has_no_direction():
    c = _check("C5", FUNDS, "VTI", "sell", 200)                    # stocks 50.0% -> 49.8%: below the 0.5-point step
    assert c.direction == "no_change" and c.direction_parts == []


def test_a_not_advisable_trade_still_reports_its_direction():
    c = _check("C3", {"VTI": 35_000, "AGG": 45_000, CASH_ID: 20_000}, "VTI", "buy", 15_000)
    assert c.verdict == "not_advisable" and c.direction == "raises_exposure" and c.direction_parts


def test_an_empty_account_has_no_direction():
    c = _check("C5", {}, "AGG", "buy", 1_000)
    assert c.before is None and c.direction == "no_change" and c.direction_parts == []


def test_reduces_breach_says_the_trade_moves_closer():
    c = _check("C5", FUNDS, "VTI", "sell", 4_000)                  # case B: VTI 50% -> 46%, limit 40%
    r = next(r for r in c.reasons if r.code == "reduces_breach")
    assert r.severity == "warning" and "moves you closer" in r.message and "46.0%" in r.message
    assert "still above the limit of 40.0%" in r.message


def test_stress_messages_show_the_scenario_label_not_its_id():
    tight = Constraint(type="max_stress_loss", value=0.05, source="behavioral", confidence=0.9,
                       scenario="equity_down_30_bond_down_5")
    c = _check("C5", FUNDS, "VTI", "buy", 1_000, behavioral_constraints=[tight])
    stress = [r for r in c.reasons if r.code == "max_stress_loss"]
    assert stress and all("Severe crash (stocks -30%)" in r.message for r in stress)
    assert all("equity_down_30_bond_down_5" not in r.message and r.scenario == "equity_down_30_bond_down_5"
               for r in stress)
