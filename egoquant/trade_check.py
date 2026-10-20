"""Trade check: is ONE planned trade in line with the investor's limits? Deterministic, no LLM.

    holdings (amounts) + PlannedTrade -> after-trade portfolio -> the limits the Risk Engine checks
    (suitability, risk-class caps, R-levels, Agent B's hard limits) + RiskEngine.review -> TradeCheck

Verdicts:
    advisable               no limit broken or worsened, nothing to warn about
    allowed_with_warnings   no limit newly broken or worsened, but something to know (a limit still exceeded
                            that this trade REDUCES, a soft limit, little room left, an expired profile, ...)
    not_advisable           the trade breaks a hard limit, or makes an existing breach worse

The chatbot (advisor/trade_explain.py) only phrases a TradeCheck. Every number it may quote is in `reasons`,
`before` and `after`; nothing is a forecast and nothing is advice to trade.

Account cash is the holding CASH_ID ("CASH", the same id as behavioral/ibkr.py's CASH_ID): asset class cash,
R1. A sale's proceeds stay in the account as cash. A buy is paid from that cash; any shortfall is treated as
new money paid in (and said so in `notes`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from egoquant.agents.market_intel import DEFAULT_CATALOG
from egoquant.models import ASSET_CLASSES, CLASS_CAP_FIELDS, AgentConfig, AssetInfo, Constraint, PortfolioProposal
from egoquant.risk_engine import RiskEngine, RiskReview
from egoquant.risk_profile import to_constraints
from egoquant.risk_profile.scoring import is_expired
from egoquant.scenarios import SCENARIO_LABELS

CASH_ID = "CASH"

Verdict = Literal["advisable", "allowed_with_warnings", "not_advisable"]

# The verdict codes are internal. Whatever an investor reads (the chat, the explanation, the Trade check page) says where the trade stands
# against their limits in these words, never "advisable" / "not advisable": a label like that is advice vocabulary even when a rule produced it.
VERDICT_WORDS = {
    "advisable": "within your limits",
    "allowed_with_warnings": "within your limits, with warnings",
    "not_advisable": "outside your limits",
}
Direction = Literal["lowers_exposure", "raises_exposure", "mixed", "no_change"]
Severity = Literal["blocking", "warning", "info"]
ReasonCode = Literal[
    "max_equity",            # stock share above a limit
    "max_class_weight",      # commodity / crypto / cash share above a limit
    "max_stress_loss",       # hypothetical loss in a stress scenario above a limit
    "r_level",               # a holding's product risk level above the investor's maximum
    "instrument_cap",        # one instrument above its own cap (e.g. 10% per single stock)
    "not_allowed",           # Agent C does not allow this product for this investor (e.g. knowledge-blocked)
    "unknown_instrument",    # not in our product list: its risk cannot be checked
    "concentration",         # one holding above the per-asset weight limit (soft)
    "min_cash",              # cash below the investor's own cash buffer (soft)
    "soft_limit",            # a soft (relaxable) behavioral limit exceeded
    "near_limit",            # inside a hard limit, but using most of the room
    "reduces_breach",        # a limit is still exceeded, but this trade reduces the excess
    "profile_expired",       # the risk questionnaire is past its validity date
    "new_money",             # the buy costs more than the account's cash
    "left_out",              # holdings not in our product list, left out of the check
    "not_checked",           # a limit that needs data the check does not have (e.g. volatility)
]
LimitSource = Literal["suitability", "risk_class", "behavioral", "investor_preference", "system", "agent_c"]


class PlannedTrade(BaseModel):
    """One trade the investor is thinking about. `amount` is money (account currency), not shares."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=15)
    side: Literal["buy", "sell"]
    amount: float = Field(gt=0, le=1e12, allow_inf_nan=False)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not v or not all(ch.isalnum() or ch in ".-" for ch in v):
            raise ValueError("ticker may contain only letters, digits, '.' and '-'")
        return v


class TradeReason(BaseModel):
    """One finding. `message` is plain English, safe to show; the numbers are fractions (0.25 = 25%)."""

    code: ReasonCode
    severity: Severity
    message: str
    source: LimitSource | None = None   # whose limit: questionnaire (suitability), risk level, Agent B, ...
    limit: float | None = None          # the limit's value, when there is one
    before: float | None = None         # the measured value before the trade
    after: float | None = None          # ... and after it
    ticker: str | None = None           # the instrument concerned, for per-instrument findings
    scenario: str | None = None         # the HYPOTHETICAL stress scenario, for stress findings


class PortfolioNumbers(BaseModel):
    """The checked portfolio, before or after the trade. Shares are fractions of `total_value`."""

    total_value: float
    weights: dict[str, float]                       # per holding, largest first
    equity_share: float
    class_shares: dict[str, float]                  # equity, bond, cash, commodity, crypto
    stress_loss: float | None                       # loss in the capacity scenario (positive = loss), HYPOTHETICAL
    stress_scenario: str                            # that scenario's name
    largest_holding: str | None
    largest_weight: float
    max_r_level: int | None                         # highest product risk level held (None = nothing assessable)


class TradeCheck(BaseModel):
    """The verdict on one planned trade, with its reasons and the before/after numbers."""

    verdict: Verdict
    planned: PlannedTrade
    as_of: date
    reasons: list[TradeReason]                      # blocking first, then warnings, then info
    before: PortfolioNumbers | None                 # None for an empty account
    after: PortfolioNumbers
    investor_max_r_level: int
    traded_r_level: int | None                      # None when the instrument is not in our product list
    risk_review: RiskReview                         # the Risk Engine's independent check of the after-trade portfolio
    notes: list[str] = Field(default_factory=list)
    # What the trade does to three measures (stock share, the capacity-scenario loss, the largest non-cash holding).
    # Descriptive only: it never changes a reason, a severity or the verdict. Not "risk": crypto, commodities and
    # R-levels are not in it. None/[] for older stored checks.
    direction: Direction | None = None
    direction_parts: list[str] = Field(default_factory=list)
    engine_version: str = "trade-check-0.1.0"
    label: str = ("deterministic check against your limits and hypothetical stress scenarios; "
                  "not a forecast and not a recommendation to trade")

    @property
    def blocking(self) -> list[TradeReason]:
        return [r for r in self.reasons if r.severity == "blocking"]


class TradeCheckError(ValueError):
    """The planned trade cannot be checked as given (e.g. selling more than is held). Message is user-safe."""




_TOL = 1e-6              # same tolerance as the Risk Engine's limit_tolerance
_DUST = 1e-9             # amounts below this are "nothing"
_NEAR_SHARE = 0.90       # PROVISIONAL: "little room left" = the measure reaches this share of a hard limit
_DEFAULT_MAX_ASSET_WEIGHT = 0.40   # InvestorContext's default per-holding cap (soft here)
_DIRECTION_MOVE = 0.005  # PROVISIONAL: a direction measure "moved" when it changes by more than half a point


class _Port:
    """The checked part of an account as weights; every limit is a function of this."""

    def __init__(self, amounts: dict[str, float], info: dict[str, AssetInfo], engine: RiskEngine):
        self.total = sum(amounts.values())
        self.weights = {a: v / self.total for a, v in amounts.items()} if self.total > _DUST else {}
        self.info, self.engine = info, engine

    def class_share(self, asset_class: str) -> float:
        return sum(w for a, w in self.weights.items() if self.info[a].asset_class == asset_class)

    def stress_loss(self, scenario_name: str) -> float | None:
        scenario = self.engine.scenarios.get(scenario_name)
        if scenario is None:
            return None
        return -sum(w * self.engine._asset_shock(self.info[a], scenario) for a, w in self.weights.items())

    def largest_non_cash(self) -> tuple[str | None, float]:
        """The largest holding other than account cash (the concentration limit's measure), ties by ticker."""
        held = sorted(((a, w) for a, w in self.weights.items() if a != CASH_ID), key=lambda kv: (-kv[1], kv[0]))
        return held[0] if held else (None, 0.0)

    def numbers(self, scenario_name: str) -> PortfolioNumbers:
        ordered = dict(sorted(self.weights.items(), key=lambda kv: (-kv[1], kv[0])))
        largest = next(iter(ordered), None)
        r_levels = [self.engine.r_levels[a] for a in ordered if a in self.engine.r_levels]
        return PortfolioNumbers(
            total_value=self.total, weights=ordered, equity_share=self.class_share("equity"),
            class_shares={c: self.class_share(c) for c in ASSET_CLASSES}, stress_loss=self.stress_loss(scenario_name),
            stress_scenario=scenario_name, largest_holding=largest, largest_weight=ordered[largest] if largest else 0.0,
            max_r_level=max(r_levels) if r_levels else None)


@dataclass
class _Limit:
    """One limit as a measurable quantity. kind "max": the measure must stay <= limit; "min": >= limit."""

    code: ReasonCode
    what: str                                  # "Your stock share", "The hypothetical loss in ..."
    limit: float
    measure: Callable[[_Port], float | None]
    hard: bool
    source: LimitSource | None
    kind: Literal["max", "min"] = "max"
    ticker: str | None = None
    scenario: str | None = None
    text: str | None = None                    # fixed wording (R-level, unchecked limits)
    holding_text: str | None = None            # R-level: what to say about a product the person already holds


_UNCHECKED = {            # limits that need the whole portfolio's history, in the client's words ("a limit on ...")
    "max_turnover": "how much of your portfolio you trade at once",
    "max_vol": "how much your portfolio's value moves up and down",
}


def _pct(x: float) -> str:
    text = f"{x:.1%}"
    return "0.0%" if text == "-0.0%" else text


def scenario_label(name: str | None) -> str:
    """The client-facing name of a stress scenario (never the raw id when a label exists)."""
    return SCENARIO_LABELS.get(name or "", name or "")


def check_trade(*, holdings: dict[str, float], planned: PlannedTrade, profile, profile_config, selection,
                assets: list[AssetInfo], behavioral_constraints: list[Constraint], as_of: date,
                **options) -> TradeCheck:
    """Check one planned trade against the investor's limits.

    holdings   account amounts by ticker (money, not weights), cash under "CASH".
    profile / profile_config   the RiskProfile and its config; selection = Agent C's UniverseSelection;
    assets     AssetInfo for the investor's universe (catalog products outside it are added from the catalog); behavioral_constraints = Agent B's (hard and soft).
    options    config (AgentConfig), scenarios (list[StressScenario]), catalog (list[Instrument]),
               min_cash_buffer, liquidity_need, max_asset_weight (default: the pipeline's), risk_engine (a ready
               RiskEngine; catalog and scenarios are then ignored).
    Raises TradeCheckError when the trade cannot be checked as given (selling more than is held, ...).
    """
    config: AgentConfig = options.get("config") or AgentConfig()
    min_cash = max(float(options.get("min_cash_buffer") or 0.0), float(options.get("liquidity_need") or 0.0))
    max_asset_weight = options.get("max_asset_weight")
    if max_asset_weight is None:
        max_asset_weight = (profile_config.adapter.r1_max_asset_weight if profile.max_r_level == 1
                            else _DEFAULT_MAX_ASSET_WEIGHT)
    engine = options.get("risk_engine") or RiskEngine(catalog=options.get("catalog") or DEFAULT_CATALOG,
                                                      scenarios=options.get("scenarios"))
    engine.r_levels.setdefault(CASH_ID, 1)
    for ticker, r in selection.r_levels.items():
        engine.r_levels.setdefault(ticker, r)
    info = {a.asset_id: a for a in assets}
    for i in options.get("catalog") or DEFAULT_CATALOG:      # a product outside the investor's universe is still known
        info.setdefault(i.ticker, AssetInfo(
            asset_id=i.ticker, asset_class=i.asset_class, expense_ratio=i.expense_ratio,
            trading_cost_bps=i.trading_cost_bps, duration_years=i.duration_years, convexity=i.convexity,
            liquidity_tier=i.liquidity_tier, max_weight=i.max_weight))
    info.setdefault(CASH_ID, AssetInfo(asset_id=CASH_ID, asset_class="cash"))

    notes: list[str] = []
    before_amounts, after_amounts, new_money = _apply_trade(_clean_holdings(holdings), planned)
    known = {a for a in info if a in engine.r_levels}
    unknown_after = sorted(a for a, v in after_amounts.items() if a not in known)
    unknown_before = sorted(a for a in before_amounts if a not in known and a not in after_amounts)
    checked_before = {a: v for a, v in before_amounts.items() if a in known}
    checked_after = {a: v for a, v in after_amounts.items() if a in known}
    before = _Port(checked_before, info, engine) if sum(checked_before.values()) > _DUST else None
    after = _Port(checked_after, info, engine)
    scenario_name = config.capacity_scenario

    reasons: list[TradeReason] = []
    traded_known = planned.ticker in known
    if planned.side == "buy":
        reasons += _buy_gate(planned, selection, profile, profile_config, traded_known, engine)
    else:
        notes.append("The sale's proceeds stay in the account as cash.")
        if not traded_known:
            reasons.append(TradeReason(
                code="unknown_instrument", severity="info", ticker=planned.ticker,
                message=f"{planned.ticker} is not in our product list, so its risk was never checked; selling it "
                        f"only turns it into cash."))
    if new_money > _DUST:
        reasons.append(TradeReason(
            code="new_money", severity="warning",
            message=f"The purchase costs {new_money:,.2f} more than the cash in the account; the check treats that "
                    f"as new money paid in."))
        notes.append(f"{new_money:,.2f} of the purchase is treated as new money paid in.")
    if unknown_after:
        many = len(unknown_after) > 1
        reasons.append(TradeReason(
            code="left_out", severity="warning",
            message=f"{', '.join(unknown_after)} {'are' if many else 'is'} not in our product list and left out of "
                    f"this check; the shares shown cover only the rest of the account."))
        notes.append("Left out of the check (not in our product list): " + ", ".join(unknown_after) + ".")
    if unknown_before:
        notes.append("Not in our product list, fully sold by this trade: " + ", ".join(unknown_before) + ".")
    if is_expired(profile, as_of):
        reasons.append(TradeReason(
            code="profile_expired", severity="warning", source="suitability",
            message="Your risk questionnaire is past its validity date; the limits used here may no longer fit you."))

    limits, engine_constraints = _limits(profile, profile_config, config, behavioral_constraints, info, engine,
                                         min_cash, max_asset_weight, sorted({*checked_before, *checked_after}), notes)
    refused = {r.ticker for r in reasons if r.code == "not_allowed"}
    for lim in limits:
        if lim.code == "r_level" and lim.ticker in refused:
            continue                                  # "not available to you" already says it
        reasons += _judge(lim, before, after)

    # The Risk Engine's own check of the after-trade portfolio has the last word.
    review = _review(engine, after, info, engine_constraints, profile, as_of)
    before_review = _review(engine, before, info, engine_constraints, profile, as_of) if before else None
    reasons = _reconcile(reasons, review, before_review)

    order = {"blocking": 0, "warning": 1, "info": 2}
    reasons.sort(key=lambda r: order[r.severity])          # stable, so the order is deterministic
    verdict: Verdict = ("not_advisable" if any(r.severity == "blocking" for r in reasons)
                        else "allowed_with_warnings" if any(r.severity == "warning" for r in reasons)
                        else "advisable")
    direction, direction_parts = _direction(before, after, scenario_name)
    return TradeCheck(verdict=verdict, planned=planned, as_of=as_of, reasons=reasons,
                      before=before.numbers(scenario_name) if before else None, after=after.numbers(scenario_name),
                      investor_max_r_level=profile.max_r_level,
                      traded_r_level=engine.r_levels.get(planned.ticker) if traded_known else None,
                      risk_review=review, notes=notes, direction=direction, direction_parts=direction_parts)


def _direction(before: _Port | None, after: _Port, scenario_name: str) -> tuple[Direction, list[str]]:
    """What the trade does to the stock share, the capacity-scenario loss and the largest non-cash holding.
    Deterministic and descriptive: all moved measures down -> lowers_exposure, all up -> raises_exposure, some each
    way -> mixed, none moved by more than _DIRECTION_MOVE -> no_change. An empty account before -> no_change."""
    if before is None:
        return "no_change", []
    moves: list[float] = []
    parts: list[str] = []

    def verb(delta: float) -> str:
        return "falls" if delta < 0 else "rises"

    b, a = before.class_share("equity"), after.class_share("equity")
    if abs(a - b) > _DIRECTION_MOVE:
        moves.append(a - b)
        parts.append(f"Your stock share {verb(a - b)} from {_pct(b)} to {_pct(a)}.")
    b, a = before.stress_loss(scenario_name), after.stress_loss(scenario_name)
    if b is not None and a is not None and abs(a - b) > _DIRECTION_MOVE:
        moves.append(a - b)
        parts.append(f"The hypothetical loss in the scenario '{scenario_label(scenario_name)}' {verb(a - b)} from "
                     f"{_pct(b)} to {_pct(a)}.")
    (tb, b), (ta, a) = before.largest_non_cash(), after.largest_non_cash()
    if abs(a - b) > _DIRECTION_MOVE:
        moves.append(a - b)
        if tb == ta:
            parts.append(f"Your largest holding, {ta}, {verb(a - b)} from {_pct(b)} to {_pct(a)} of the account.")
        else:
            parts.append(f"Your largest single holding {verb(a - b)} from {_pct(b)} ({tb}) to {_pct(a)} "
                         f"({ta or 'none'}) of the account.")
    if not moves:
        return "no_change", []
    if all(m < 0 for m in moves):
        return "lowers_exposure", parts
    if all(m > 0 for m in moves):
        return "raises_exposure", parts
    return "mixed", parts


# ---------------------------------------------------------------------------------------------------------
def _clean_holdings(holdings: dict[str, float]) -> dict[str, float]:
    clean: dict[str, float] = {}
    for key, value in holdings.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise TradeCheckError("Holdings must be non-negative amounts of money.")
        ticker = str(key).strip().upper()
        clean[ticker] = clean.get(ticker, 0.0) + float(value)
    return {k: v for k, v in clean.items() if v > _DUST}


def _apply_trade(holdings: dict[str, float], planned: PlannedTrade) -> tuple[dict, dict, float]:
    """-> (before amounts, after amounts, new money paid in)."""
    if planned.ticker == CASH_ID:
        raise TradeCheckError("Cash is not a product to trade; enter the purchase or sale of a product.")
    after = dict(holdings)
    new_money = 0.0
    if planned.side == "sell":
        held = holdings.get(planned.ticker, 0.0)
        if held <= _DUST:
            raise TradeCheckError(f"You do not hold {planned.ticker}, so it cannot be sold.")
        if planned.amount > held * (1 + 1e-9) + _DUST:
            raise TradeCheckError(f"You hold {held:,.2f} of {planned.ticker}; selling {planned.amount:,.2f} is more "
                                  f"than that.")
        sold = min(planned.amount, held)
        after[planned.ticker] = held - sold
        after[CASH_ID] = after.get(CASH_ID, 0.0) + sold
    else:
        cash = holdings.get(CASH_ID, 0.0)
        paid_from_cash = min(cash, planned.amount)
        new_money = planned.amount - paid_from_cash
        after[CASH_ID] = cash - paid_from_cash
        after[planned.ticker] = after.get(planned.ticker, 0.0) + planned.amount
    return holdings, {k: v for k, v in after.items() if v > _DUST}, new_money


def _buy_gate(planned: PlannedTrade, selection, profile, profile_config, traded_known: bool,
              engine: RiskEngine) -> list[TradeReason]:
    t = planned.ticker
    if not traded_known:
        return [TradeReason(code="unknown_instrument", severity="blocking", ticker=t, source="system",
                            message=f"{t} is not in our product list, so its risk level cannot be checked; it is not "
                                    f"treated as allowed.")]
    r_level = engine.r_levels.get(t)
    if r_level is not None and r_level > profile.max_r_level:     # a category, not a matter of how much
        return [TradeReason(code="not_allowed", severity="blocking", ticker=t, source="suitability",
                            message=f"{t} is a higher-risk product (risk level R{r_level}); your category "
                                    f"({profile.c_level}) allows products up to R{profile.max_r_level}, so it is not "
                                    f"available to you.")]
    refused = {e.ticker: e.reason for e in selection.excluded}
    if t in selection.assets_to_exclude or t in refused:
        why = refused.get(t, "it is not allowed for your profile")
        return [TradeReason(code="not_allowed", severity="blocking", ticker=t, source="agent_c",
                            message=f"{t} is not available to you: {why}.")]
    if profile.product_access == "blocked_complex":
        product_type = next((i.product_type for i in [*selection.instruments, *selection.watchlist]
                             if i.ticker == t), None) or next((i.product_type for i in DEFAULT_CATALOG
                                                               if i.ticker == t), None)
        if product_type in set(profile_config.knowledge.complex_product_types):
            return [TradeReason(code="not_allowed", severity="blocking", ticker=t, source="suitability",
                                message=f"{t} is a {product_type} product; your questionnaire answers on product "
                                        f"knowledge do not allow those.")]
    return []


def _limits(profile, profile_config, config: AgentConfig, behavioral: list[Constraint], info: dict[str, AssetInfo],
            engine: RiskEngine, min_cash: float, max_asset_weight: float, tickers: list[str],
            notes: list[str]) -> tuple[list[_Limit], list[Constraint]]:
    """The limits the pipeline hands the Risk Engine, plus per-holding and soft ones.
    Returns (limits to judge, the hard Constraints for RiskEngine.review)."""
    policy = config.risk_class_policies[profile.c_level]
    constraints: list[Constraint] = list(to_constraints(profile, profile_config))
    constraints += [Constraint(type="max_equity", value=policy.max_equity, source="risk_class"),
                    Constraint(type="max_stress_loss", value=policy.max_stress_loss, source="risk_class",
                               scenario=config.capacity_scenario)]
    for asset_class, field_name in CLASS_CAP_FIELDS.items():
        cap = getattr(policy, field_name)
        if asset_class == "cash":
            # CLAUDE.md decision 14: Agent A caps cash only so that a minimum-variance optimizer does not park a growth
            # portfolio in T-bills. That is a portfolio-construction rule, not investor protection (more cash cannot
            # raise the stress loss or an R-level), so the trade check does not apply it to the investor's own trades
            # (branch trade-check-cash). It is therefore also out of the Risk Engine review below. The investor's own
            # cash need still applies: the min_cash floor at the end of this function.
            if cap < 1.0:
                notes.append(f"The portfolio builder's cash cap ({cap:.0%}) is a construction rule and is not "
                             f"applied to your own trades.")
            continue
        if cap < 1.0:
            constraints.append(Constraint(type="max_class_weight", value=cap, source="risk_class",
                                          asset_class=asset_class))
    weak = [c for c in behavioral if c.confidence is not None and c.confidence < config.min_behavioral_confidence]
    if weak:                                          # Agent A ignores weak evidence too
        notes.append(f"{len(weak)} behavioral limit(s) below the {config.min_behavioral_confidence:.0%} confidence "
                     f"threshold were not applied, as in the portfolio build.")
    constraints += [c for c in behavioral if c not in weak]

    limits: list[_Limit] = []
    for c in constraints:
        if c.type == "max_equity":
            lim = _Limit("max_equity", "Your stock share", c.value, lambda p: p.class_share("equity"), c.hard,
                         c.source)
        elif c.type == "max_class_weight":
            ac = c.asset_class
            lim = _Limit("max_class_weight", f"Your {ac} share", c.value, lambda p, ac=ac: p.class_share(ac), c.hard,
                         c.source)
        elif c.type == "min_class_weight":
            ac = c.asset_class
            lim = _Limit("soft_limit", f"Your {ac} share", c.value, lambda p, ac=ac: p.class_share(ac), c.hard,
                         c.source, kind="min")
        elif c.type == "max_stress_loss":
            sc = c.scenario
            lim = _Limit("max_stress_loss", f"The hypothetical loss in the scenario '{scenario_label(sc)}'", c.value,
                         lambda p, sc=sc: p.stress_loss(sc), c.hard, c.source, scenario=sc)
        elif c.type == "max_asset_weight" and c.asset_id:
            tk = c.asset_id
            lim = _Limit("concentration", f"Your {tk} share", c.value, lambda p, tk=tk: p.weights.get(tk, 0.0),
                         c.hard, c.source, ticker=tk)
        elif c.type == "max_asset_weight":
            lim = _Limit("concentration", "Your largest holding", c.value,
                         lambda p: max((w for a, w in p.weights.items() if a != CASH_ID), default=0.0), c.hard,
                         c.source)
        elif c.type == "min_cash":
            lim = _Limit("min_cash", "Your cash share", c.value, lambda p: p.class_share("cash"), c.hard, c.source,
                         kind="min")
        else:                                         # max_vol, max_turnover: need data this check does not have
            what = _UNCHECKED.get(c.type, "your whole portfolio")
            shown = f" ({c.value:.0%})" if isinstance(c.value, (int, float)) and 0 <= c.value <= 1 else ""
            notes.append(f"Not checked for a single trade: your limit on {what}.")
            limits.append(_Limit("not_checked", c.type, c.value, lambda p: None, c.hard, c.source,
                                 text=f"A limit on {what}{shown} cannot be checked for a single trade, so it was skipped."))
            continue
        if not c.hard:
            lim.code = "soft_limit"
        limits.append(lim)

    for t in tickers:                                 # per holding: R-level, own cap, concentration
        r = engine.r_levels.get(t)
        if r is not None and r > profile.max_r_level:
            limits.append(_Limit("r_level", f"Your {t} share", 0.0, lambda p, t=t: p.weights.get(t, 0.0), True,
                                 "suitability", ticker=t,
                                 text=(f"{t} is a higher-risk product (risk level R{r}); your category ({profile.c_level}) allows "
                                       f"products up to R{profile.max_r_level}"),
                                 holding_text=(f"a higher-risk product (risk level R{r}); your category ({profile.c_level}) "
                                               f"allows products up to R{profile.max_r_level}")))
        if info[t].max_weight is not None:
            limits.append(_Limit("instrument_cap", f"Your {t} share", info[t].max_weight,
                                 lambda p, t=t: p.weights.get(t, 0.0), True, "agent_c", ticker=t))
        if t != CASH_ID:
            limits.append(_Limit("concentration", f"Your {t} share", max_asset_weight,
                                 lambda p, t=t: p.weights.get(t, 0.0), False, "system", ticker=t))
    if min_cash > 0:
        limits.append(_Limit("min_cash", "Your cash share", min_cash, lambda p: p.class_share("cash"), False,
                             "investor_preference", kind="min"))
    limits = _tightest(limits)
    engine_constraints = [c for c in constraints
                          if c.hard and c.type in ("max_equity", "max_class_weight", "max_stress_loss")]
    return limits, engine_constraints


def _tightest(limits: list[_Limit]) -> list[_Limit]:
    """One finding per measured quantity: of several limits on the same thing (e.g. the suitability cap and the
    risk-class cap on stocks), only the strictest is judged; the first listed wins a tie."""
    best: dict[tuple, _Limit] = {}
    for lim in limits:
        key = (lim.code, lim.what, lim.ticker, lim.scenario, lim.kind, lim.hard, lim.text)
        old = best.get(key)
        if old is None or (lim.limit < old.limit if lim.kind == "max" else lim.limit > old.limit):
            best[key] = lim
    return [lim for lim in limits if best[(lim.code, lim.what, lim.ticker, lim.scenario, lim.kind, lim.hard,
                                           lim.text)] is lim]


def _judge(lim: _Limit, before: _Port | None, after: _Port) -> list[TradeReason]:
    """Compare one limit before and after the trade -> zero or one reasons."""
    if lim.code == "not_checked":
        return [TradeReason(code="not_checked", severity="warning" if lim.hard else "info", source=lim.source,
                            limit=lim.limit, message=lim.text or "")]
    a = lim.measure(after)
    if a is None:
        return [TradeReason(code="not_checked", severity="warning", source=lim.source, scenario=lim.scenario,
                            message=f"{lim.what} could not be measured (unknown scenario), so it was not checked.")]
    b = lim.measure(before) if before is not None else None
    sign = 1.0 if lim.kind == "max" else -1.0
    word = "limit" if lim.kind == "max" else "minimum"
    side = "above" if lim.kind == "max" else "below"
    what_limit = "what your category allows (none of it)" if lim.code == "r_level" else f"the {word} of {_pct(lim.limit)}"
    severity = "blocking" if lim.hard else "warning"
    now = f" (now {_pct(b)})" if b is not None else ""

    def make(code, sev, message):
        return [TradeReason(code=code, severity=sev, message=message, source=lim.source, limit=lim.limit, before=b,
                            after=a, ticker=lim.ticker, scenario=lim.scenario)]

    if sign * (a - lim.limit) > _TOL:                 # still / newly outside the limit
        was_over = b is not None and sign * (b - lim.limit) > _TOL
        moved = sign * (a - b) if b is not None else None          # > 0: further outside
        if not was_over:
            if lim.text:
                return make(lim.code, severity, f"{lim.text}; this trade would give it {_pct(a)} of the account.")
            return make(lim.code, severity,
                        f"{lim.what} would be {_pct(a)}, {side} the {word} of {_pct(lim.limit)}{now}.")
        if lim.code == "r_level" and lim.holding_text:
            held = f"You hold {_pct(b)} in {lim.ticker}, {lim.holding_text}."
            if moved > _TOL:
                return make(lim.code, severity, f"{held} This trade makes it {_pct(a)}.")
            if moved < -_TOL:
                return make("reduces_breach", "warning",
                            f"{held} This trade moves you closer: it becomes {_pct(a)}. Your category allows none of it.")
            return make(lim.code, "warning", f"{held} This trade does not change that.")
        subject = lim.text or f"{lim.what} is already {side} {what_limit}"
        if moved > _TOL:
            return make(lim.code, severity,
                        f"{subject} ({_pct(b)}); this trade makes it {_pct(a)}.")
        if moved < -_TOL:
            return make("reduces_breach", "warning",
                        f"{subject} ({_pct(b)}). This trade moves you closer: it becomes {_pct(a)}. It is still "
                        f"{side} {what_limit}.")
        return make(lim.code, "warning", f"{subject} ({_pct(a)}); this trade does not change that.")
    # Inside the limit: warn when this trade uses up most of a hard limit's room.
    if lim.hard and lim.kind == "max" and lim.limit > _TOL and a >= _NEAR_SHARE * lim.limit \
            and (b is None or a - b > _TOL):
        return make("near_limit", "warning",
                    f"{lim.what} would be {_pct(a)}, close to the {word} of {_pct(lim.limit)}{now}.")
    return []


def _review(engine: RiskEngine, port: _Port, info: dict[str, AssetInfo], constraints: list[Constraint], profile,
            as_of: date) -> RiskReview:
    proposal = PortfolioProposal(stage="constrained", method="trade-check", as_of=as_of, status="optimal",
                                 weights=dict(port.weights))
    return engine.review(proposal, list(info.values()), constraints, profile.max_r_level)


def _reconcile(reasons: list[TradeReason], review: RiskReview, before_review: RiskReview | None) -> list[TradeReason]:
    """Never 'advisable' while the Risk Engine rejects the after-trade portfolio, and never lenient about a check
    this trade newly breaks."""
    failing = {c.name: c.detail for c in review.checks if not c.passed}
    if not failing:
        return reasons
    was_failing = {c.name for c in before_review.checks if not c.passed} if before_review else set()
    newly = {n: d for n, d in failing.items() if n not in was_failing}
    out = list(reasons)
    if newly and not any(r.severity == "blocking" for r in out):
        name, detail = sorted(newly.items())[0]
        out.append(TradeReason(code=_code_for_check(name), severity="blocking", source="system",
                               message=f"The Risk Engine's check fails after this trade: {detail}."))
    elif not any(r.severity in ("blocking", "warning") for r in out):
        name, detail = sorted(failing.items())[0]
        out.append(TradeReason(code="reduces_breach", severity="warning", source="system",
                               message=f"The Risk Engine still reports: {detail}."))
    return out


def _code_for_check(name: str) -> ReasonCode:
    if name.startswith("r_level"):
        return "r_level"
    if name.startswith("instrument_cap"):
        return "instrument_cap"
    if name == "asset_metadata":
        return "unknown_instrument"
    if "max_equity" in name:
        return "max_equity"
    if "max_class_weight" in name:
        return "max_class_weight"
    return "max_stress_loss"
