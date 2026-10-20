"""Client-facing report for Agent A: turns a result into numbers, reasons and one HTML page.

Two layers, both deterministic (no LLM, same input -> same output):

    build_client_report(...)  -> ClientReport   structured data (JSON-safe): what the app or the
                                                Advisor chatbot reads. Every number and every
                                                reason sentence is produced here, from the result.
    render_html(report)       -> str            one self-contained HTML page with inline SVG charts
                                                (works offline, prints to PDF from any browser).

Why this shape: a chatbot may REPHRASE the report, but it must not invent numbers or reasons.
Keeping the facts in ClientReport means the wording can be friendly while the content stays auditable.

Sections follow what wealth-management proposals and the EU key-information documents (KID) usually show:
summary, what you would hold, why, how risky, what could happen, options considered, changes and costs,
assumptions and disclosures. Agent A makes NO return forecast, so the report shows none.

With a goal (the goal-based path), a "Your goal" section follows the summary (build_goal_report). Its
presentation follows the research on communicating risk and simulated outcomes:
  * chances as natural frequencies ("in 69 of 100 simulated futures") with a 100-icon array: frequencies and
    icon arrays are understood better than percentages, most of all by readers with low numeracy
    (Gigerenzer & Hoffrage 1995, Psychological Review 102; Spiegelhalter, Pearson & Short 2011, "Visualizing
    uncertainty about the future", Science 333);
  * outcomes labelled by how often they happen ("1 in 10 futures ends below ..."), never "pessimistic /
    moderate / optimistic": readers take the middle scenario for the expected one (the EU PRIIPs consumer
    testing behind the 2021 KID scenario review);
  * the size of a miss in money, not only its probability (the worst-5% shortfall, falls along the way,
    money running out): a success rate alone hides how bad failure is (Kitces, "Monte Carlo
    probability of success vs. magnitude of failure");
  * rounded numbers and the honest uncertainty (sampling error, and how much the answer depends on the
    market period): no false precision;
  * what the investor can change (monthly amount, goal date, goal amount), re-simulated on the same paths;
  * the method in plain words and disclosures in the spirit of FINRA Rule 2214 for investment-analysis tools:
    hypothetical, not a guarantee, results vary with each use and over time, the assumptions and limits stated.
"""

from __future__ import annotations

import html
import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_serializer

from egoquant.agents.market_intel import UniverseSelection
from egoquant.agents.quant.goal import FanPoint, GoalAnalysis, PeriodCheck
from egoquant.agents.quant.v2 import method_names as v2_method_names
from egoquant.agents.market_intel.agent import DrawdownAlert
from egoquant.briefing.advice_filter import SAFE_REPLACEMENT, AdviceFilter
from egoquant.briefing.aggregate import RECHECK_OFFER
from egoquant.briefing.models import Briefing
from egoquant.models import (
    ASSET_CLASSES,
    CLASS_CAP_FIELDS,
    DEFAULT_RISK_CLASS_POLICIES,
    AgentAResult,
    AgentConfig,
    AssetInfo,
    InvestorContext,
    StressScenario,
)
from egoquant.risk_engine import RiskReview, plain_violations
from egoquant.risk_profile.models import RiskProfile
from egoquant.risk_profile.scoring import is_expired
from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS, SCENARIO_LABELS

DEFAULT_ASSET_NAMES: dict[str, str] = {
    "EQ_GLOBAL": "Global equities", "EQ_EMERGING": "Emerging-market equities", "EQ_VALUE": "Value equities",
    "BOND_GOVT": "Government bonds", "BOND_CORP": "Corporate bonds", "CASH_MM": "Money-market fund (cash)",
}
CLASS_LABEL = {"equity": "Stocks", "bond": "Bonds", "cash": "Cash", "commodity": "Gold & commodities",
               "crypto": "Crypto"}
ESTIMATOR_LABELS = {"ledoit_wolf": "a standard statistical method (Ledoit-Wolf)", "ewma": "exponentially weighted averages",
                    "sample": "plain sample averages"}


# ==========================================================================
# Report data model
# ==========================================================================
class AllocationRow(BaseModel):
    asset_id: str
    name: str
    asset_class: str
    weight: float
    amount: float                      # money for the reference investment amount
    expense_ratio: float               # annual fund fee, fraction
    risk_share: float                  # share of portfolio variance this holding contributes


class LimitCheck(BaseModel):
    name: str
    value: float                       # what the portfolio has
    limit: float                       # the limit it must respect
    direction: str                     # "max" (value must stay below) or "min" (value must stay above)
    ok: bool
    source: str                        # where the limit comes from, in words


class StressRow(BaseModel):
    name: str
    description: str
    portfolio_return: float            # HYPOTHETICAL, negative = loss
    amount_change: float               # same, in money for the reference amount


class MenuPoint(BaseModel):
    kind: str                          # "option", "class_default", "baseline", "recommended"
    equity: float
    vol: float
    worst_loss: float                  # worst hypothetical scenario loss, positive number


class TradeRow(BaseModel):
    asset_id: str
    name: str
    current: float
    proposed: float
    change: float


class Reason(BaseModel):
    kind: str                          # "class", "method", "limit", "behavioral", "ignored", "relaxed", ...
    text: str


class GoalRow(BaseModel):
    """One line of the goal section: a short label, the number as shown, and the plain sentence."""

    label: str
    value: str
    text: str


class GoalLever(BaseModel):
    kind: str                          # "monthly" | "horizon" | "goal"
    change: str                        # what changes, e.g. "Add 470 USD a month instead of 200 USD"
    chance: str                        # the chance with that change, e.g. "90%"
    prob_goal: float


class GoalStep(BaseModel):
    title: str
    text: str


class GoalShortlistRow(BaseModel):
    label: str
    equity_weight: float
    prob_goal: float
    p50: float
    worst5_shortfall: float
    chosen: bool


class GoalReport(BaseModel):
    """The goal simulation, as the investor reads it. HYPOTHETICAL: past data, not a forecast."""

    label: str                         # the hypothetical label every view shows
    goal_amount: float
    start_amount: float
    monthly_flow: float                # + contribution, - withdrawal
    horizon_years: float
    total_contributed: float
    prob_goal: float                   # the chosen portfolio's chance (share of simulated futures)
    in_100: int                        # icons filled in the 100-icon array (never 0 or 100 unless exact)
    chance: str                        # the chance as shown: "69%", "less than 1%", "more than 99%"
    headline: str                      # the chance as a natural frequency, in one sentence
    plan: str                          # the plan in words (start, monthly amount, goal, years)
    precision: str                     # sampling error and how much the market period matters
    outcomes: list[GoalRow]            # final amounts, labelled by how often they happen
    severity: list[GoalRow]            # how bad a miss can be, in money
    fan: list[FanPoint]                # wealth percentiles per year (chosen portfolio)
    levers_intro: str
    levers: list[GoalLever]            # what would change the odds (one input at a time, same futures)
    method: list[GoalStep]             # how the chance was calculated, in plain words
    choice: list[str]                  # how this portfolio was chosen
    shortlist: list[GoalShortlistRow]  # Agent B's shortlist, best first
    period_checks: list[PeriodCheck]   # older half vs newer half of the history
    std_error: float
    n_paths: int
    history_start: str
    history_end: str
    disclosures: list[str]


class ClientReport(BaseModel):
    status: str                        # "optimal", "fallback" or "infeasible"
    investor_id: str
    as_of: date
    currency: str
    reference_amount: float
    risk_class: str
    headline: str
    equity_weight: float = 0.0
    bond_weight: float = 0.0
    cash_weight: float = 0.0
    commodity_weight: float = 0.0
    crypto_weight: float = 0.0
    ex_ante_vol: float = 0.0
    worst_loss: float = 0.0            # worst hypothetical scenario loss (positive), fraction
    worst_scenario: str = ""
    limit_scenario: str = ""          # the one scenario your loss limit applies to
    limits_ok: bool = True
    allocation: list[AllocationRow] = Field(default_factory=list)
    limits: list[LimitCheck] = Field(default_factory=list)
    stress: list[StressRow] = Field(default_factory=list)
    reverse_stress_equity_drop: float | None = None
    menu: list[MenuPoint] = Field(default_factory=list)
    trades: list[TradeRow] = Field(default_factory=list)
    turnover: float | None = None
    one_off_trading_cost: float | None = None          # fraction of portfolio value
    ongoing_fee: float = 0.0                           # weighted annual fund fee, fraction
    reasons: list[Reason] = Field(default_factory=list)
    risk_facts: dict[str, float] = Field(default_factory=dict)
    banners: list[str] = Field(default_factory=list)   # loud warnings (demo data, placeholder policy, ...)
    market_briefing: Briefing | None = None            # optional; shown alongside, never changes the portfolio
    what_changed: list[str] = Field(default_factory=list)   # with a briefing: drawdowns (Agent C) + tone (Agent D)
    what_changed_action: Literal["offer_risk_profile_recheck"] | None = None   # the ONLY action that exists
    profile_assessed_on: date | None = None             # risk questionnaire completed on
    profile_valid_until: date | None = None             # re-assessment due after this day
    profile_expired: bool = False                       # valid_until before as_of: flagged, never silently used
    risk_review_approved: bool | None = None           # independent Risk Engine verdict; None = not reviewed
    risk_review_violations: list[str] = Field(default_factory=list)
    interest_notes: list[str] = Field(default_factory=list)   # what happened to each area the investor asked about
    goal: GoalReport | None = None                     # goal path only: the simulation, as the investor reads it
    # Only when the Risk Game was played (docs/RISK_GAME_BLEND.md): which parts set the category. Left out of the JSON otherwise.
    category_basis: dict[str, float | int | str] | None = None
    # Portfolio method v2 only (docs/PORTFOLIO_V2.md section 4): the flat v2_* diagnostics of the chosen portfolio. Left out of the JSON
    # when empty, so the report of the default engine is byte-identical to before.
    final_diagnostics: dict[str, float | str] | None = None
    disclosures: list[str] = Field(default_factory=list)
    data_source: str = ""
    prices_as_of: date | None = None                   # set when the committed snapshot replaced the live feed: the day its prices end
    price_note: str | None = None                      # e.g. "real prices as of 2026-10-16, cached (Yahoo Finance; ...)"
    engine_version: str = ""
    input_hash: str = ""

    # ONE serializer for every optional field that is left out of the JSON when empty: pydantic keeps only the LAST
    # @model_serializer of a model, so two of them would silently drop the first one's work (tests/test_report_serializers.py).
    @model_serializer(mode="wrap")
    def _leave_out_empty_optional_fields(self, handler):
        """Left out of the dump unless set, so a report built on live or synthetic prices with the default portfolio method
        serialises exactly as before (golden hashes, saved reports): the v2 diagnostics (portfolio method v2 only) and the
        two price fields (only when the committed snapshot replaced the live feed)."""
        data = handler(self)
        if not data.get("final_diagnostics"):
            data.pop("final_diagnostics", None)
        if not data.get("category_basis"):
            data.pop("category_basis", None)
        for key in ("prices_as_of", "price_note"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


# ==========================================================================
# Building the report
# ==========================================================================
def build_client_report(
    result: AgentAResult,
    context: InvestorContext,
    assets: list[AssetInfo],
    config: AgentConfig | None = None,
    scenarios: list[StressScenario] | None = None,
    reference_amount: float = 10_000.0,
    asset_names: dict[str, str] | None = None,
    risk_review: RiskReview | None = None,
    selection: UniverseSelection | None = None,
    briefing: Briefing | None = None,
    risk_profile: RiskProfile | None = None,
    goal_analysis: GoalAnalysis | None = None,
    screening=None,
) -> ClientReport:
    """Collect numbers, limit checks and plain-language reasons from one AgentAResult.

    `assets` is the list Agent C supplied (needed for asset classes and fees).
    `config` and `scenarios` should be the ones the agent ran with (defaults are used if omitted).
    `selection` is Agent C's universe for this investor; with it the report explains, for every interest
    area the investor named, what is held, what is allowed but not bought, and what is not allowed.
    `risk_review` is the Risk Engine's verdict on result.final. It is stored in the report; the page
    mentions it only when the proposal was NOT approved.
    `briefing` (optional) is the market briefing shown alongside the portfolio. It is stored and rendered
    only; it does not change any number above.
    `goal_analysis` and `screening` (goal path): the simulation and Agent B's shortlist; they become the
    report's "Your goal" section (build_goal_report).
    """
    config = config or AgentConfig()
    scenarios = scenarios or DEFAULT_STRESS_SCENARIOS
    names = {**DEFAULT_ASSET_NAMES, **(asset_names or {})}
    final = result.final
    base = dict(status=final.status, investor_id=context.investor_id, as_of=final.as_of,
                currency=context.base_currency, reference_amount=reference_amount, risk_class=context.risk_class,
                data_source=final.data_source, engine_version=final.engine_version, input_hash=final.input_hash,
                risk_review_approved=risk_review.approved if risk_review else None,
                risk_review_violations=list(risk_review.violations) if risk_review else [])

    if final.status == "infeasible" or final.risk is None:
        return attach_briefing(briefing, _with_profile(risk_profile, ClientReport(**base, headline="We could not build a portfolio that respects all of your limits. "
                            "No portfolio is proposed.",
                            reasons=[Reason(kind="infeasible", text=final.message or "No feasible portfolio.")],
                            banners=_banners(final.data_source, config, risk_review), disclosures=_disclosures(config))))

    info = {a.asset_id: a for a in assets}
    policy = config.risk_class_policies[context.risk_class]
    weights = final.weights

    # ---- split by asset class, allocation rows ----------------------------------------------
    split = {c: 0.0 for c in ASSET_CLASSES}
    rows: list[AllocationRow] = []
    for asset_id, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if w < 1e-6 or asset_id not in info:
            continue
        a = info[asset_id]
        split[a.asset_class] += w
        rows.append(AllocationRow(asset_id=asset_id, name=names.get(asset_id, asset_id), asset_class=a.asset_class,
                                  weight=w, amount=w * reference_amount, expense_ratio=a.expense_ratio,
                                  risk_share=final.risk.risk_contributions.get(asset_id, 0.0)))
    ongoing_fee = sum(w * info[a].expense_ratio for a, w in weights.items() if a in info)

    # ---- stress rows -------------------------------------------------------------------------
    by_name = {s.name: s for s in scenarios}
    stress_rows = [StressRow(name=SCENARIO_LABELS.get(n, n.replace("_", " ").capitalize()), description=by_name[n].description if n in by_name else n,
                             portfolio_return=r, amount_change=r * reference_amount)
                   for n, r in final.stress.items()]
    worst_name = min(final.stress, key=final.stress.get) if final.stress else ""
    worst_loss = max(0.0, -final.stress[worst_name]) if worst_name else 0.0

    # ---- limit checks (the investor's limits, in numbers) -----------------------------------
    behavioral = {c.label: c for c in result.behavioral_constraints}
    ignored = set(final.ignored_constraints)
    capacity_loss = max(0.0, -final.stress.get(config.capacity_scenario, 0.0))
    cap_scenario_text = by_name[config.capacity_scenario].description if config.capacity_scenario in by_name else ""
    limits = [
        _check("Share in stocks", split["equity"], min(policy.max_equity, context.suitability_max_equity), "max",
               f"your category {context.risk_class} and your answers"),
        _check("Estimated volatility", final.risk.ex_ante_vol, policy.max_vol, "max", f"your category {context.risk_class}"),
        _check("Loss in a severe fall", capacity_loss,
               min(policy.max_stress_loss,
                   context.max_acceptable_loss if context.max_acceptable_loss is not None else 1.0), "max",
               f"your category and the loss you said you can afford ({cap_scenario_text.lower()})"),
        _check("Largest single holding", max(weights.values()), context.max_asset_weight, "max", _holding_cap_source(context)),
    ]
    min_cash = max(context.min_cash_buffer, context.liquidity_need)
    if min_cash > 0:
        limits.append(_check("Cash buffer", split["cash"], min_cash, "min", "your liquidity needs"))
    for asset_class, field_name in CLASS_CAP_FIELDS.items():
        cap = getattr(policy, field_name)
        if asset_class == "cash":
            cap = max(cap, min_cash)
        if cap < 1.0 and any(a.asset_class == asset_class for a in assets):
            limits.append(_check(f"Share in {CLASS_LABEL[asset_class].lower()}", split[asset_class], cap, "max",
                                 f"your category {context.risk_class}"))
    relaxed = set(final.relaxed_constraints)
    # The crypto the investor asked for: Agent A's small soft minimum, always inside the class cap.
    crypto_assets = {a.asset_id for a in assets if a.asset_class == "crypto"}
    if (config.interest_crypto_floor > 0 and policy.max_crypto > 0 and set(context.interest_assets) & crypto_assets
            and "investor_preference:min_class_weight[crypto]" not in relaxed):
        limits.append(_check("Crypto you asked for (minimum)", split["crypto"],
                             min(config.interest_crypto_floor, policy.max_crypto), "min",
                             "you chose crypto: a small minimum, always inside your category's limit"))
    for label, c in behavioral.items():
        if label in ignored or label in relaxed:          # not applied; explained in the reasons instead
            continue
        why = (f"what your past choices suggest (confidence {c.confidence:.0%})"
               if c.confidence is not None else "what your past choices suggest")
        if c.type == "max_equity":
            limits.append(_check("Stocks, behavioural limit", split["equity"], c.value, "max", why))
        elif c.type == "max_stress_loss" and c.scenario in final.stress:
            scenario_text = by_name[c.scenario].description.lower() if c.scenario in by_name else c.scenario
            limits.append(_check("Loss in a severe fall, behavioural limit", max(0.0, -final.stress[c.scenario]),
                                 c.value, "max", f"{why} ({scenario_text})"))
        elif c.type == "max_turnover" and final.implementation is not None:
            limits.append(_check("Share of the portfolio traded, behavioural limit", final.implementation.turnover,
                                 c.value, "max", why))

    # ---- candidate menu points (for the "options considered" chart) ------------------------------
    menu_points = [MenuPoint(kind="class_default" if i == result.menu.default_index else "option",
                             equity=p.equity_weight or 0.0, vol=p.risk.ex_ante_vol,
                             worst_loss=max(0.0, -min(p.stress.values())))
                   for i, p in enumerate(result.menu.portfolios) if p.risk is not None and p.status != "infeasible"]
    b = result.menu.baseline
    if b.risk is not None:
        menu_points.append(MenuPoint(kind="baseline", equity=b.equity_weight or 0.0, vol=b.risk.ex_ante_vol,
                                     worst_loss=max(0.0, -min(b.stress.values()))))
    menu_points.append(MenuPoint(kind="recommended", equity=split["equity"], vol=final.risk.ex_ante_vol,
                                 worst_loss=worst_loss))

    # ---- trades versus current holdings ----------------------------------------------------------
    trades: list[TradeRow] = []
    if context.current_holdings is not None:
        ids = sorted(set(context.current_holdings) | set(weights))
        trades = [TradeRow(asset_id=a, name=names.get(a, a), current=context.current_holdings.get(a, 0.0),
                           proposed=weights.get(a, 0.0), change=weights.get(a, 0.0) - context.current_holdings.get(a, 0.0))
                  for a in ids if max(context.current_holdings.get(a, 0.0), weights.get(a, 0.0)) > 1e-6]

    headline = (f"We propose {_mix_phrase(split)}. "
                f"Its estimated volatility (the typical size of its yearly ups and downs) is about {final.risk.ex_ante_vol:.1%}. "
                f"In the harshest of our hypothetical scenarios it would lose about {worst_loss:.0%} "
                f"(roughly {worst_loss * reference_amount:,.0f} {context.base_currency} per {reference_amount:,.0f} invested).")

    return attach_briefing(briefing, _with_profile(risk_profile, ClientReport(
        **base, headline=headline, equity_weight=split["equity"], bond_weight=split["bond"], cash_weight=split["cash"],
        commodity_weight=split["commodity"], crypto_weight=split["crypto"],
        ex_ante_vol=final.risk.ex_ante_vol, worst_loss=worst_loss, worst_scenario=SCENARIO_LABELS.get(worst_name, worst_name.replace("_", " ")),
        limit_scenario=SCENARIO_LABELS.get(config.capacity_scenario, config.capacity_scenario),
        limits_ok=all(c.ok for c in limits) and not final.violated_constraints,
        allocation=rows, limits=limits, stress=stress_rows, reverse_stress_equity_drop=final.reverse_stress_equity_drop,
        menu=menu_points, trades=trades,
        turnover=final.implementation.turnover if final.implementation else None,
        one_off_trading_cost=final.implementation.estimated_cost if final.implementation else None,
        ongoing_fee=ongoing_fee,
        reasons=_reasons(result, context, config, policy, split, by_name, names, assets, screening),
        interest_notes=_interest_notes(selection, weights, policy, config, names) if selection else [],
        goal=(build_goal_report(goal_analysis, result, screening, context.base_currency, config.goal_tie_tolerance)
              if goal_analysis else None),
        risk_facts={"stressed_correlation_vol": final.risk.stressed_corr_vol, "hist_var_1d": final.risk.hist_var,
                    "hist_cvar_1d": final.risk.hist_cvar, "hist_max_drawdown": final.risk.hist_max_drawdown,
                    "effective_holdings": final.risk.effective_n_holdings,
                    "diversification_ratio": final.risk.diversification_ratio},
        banners=_banners(final.data_source, config, risk_review), disclosures=_disclosures(config),
        final_diagnostics={k: v for k, v in final.diagnostics.items() if k.startswith("v2_")} or None,
    )))


def attach_briefing(briefing: Briefing | None, report: ClientReport) -> ClientReport:
    """Attach an optional market briefing to a finished report (numbers unchanged). Its banners (e.g. the DEMO notice) go to the top of the page."""
    if briefing is None:
        return report
    banners = [*briefing.banners, *(b for b in report.banners if b not in briefing.banners)]
    return report.model_copy(update={"market_briefing": briefing, "banners": banners})




def what_changed_lines(drawdown_alerts: list[DrawdownAlert], briefing: Briefing | None,
                       names: dict[str, str] | None = None, data_to: date | None = None,
                       synthetic: bool = False, holdings: list[str] | None = None,
                       interest_tickers: set[str] | None = None, drawdown_threshold: float | None = None) -> list[str]:
    """Plain sentences built from the numbers: Agent C's drawdown alerts, then Agent D's tone alerts.

    HOLDINGS ONLY: drawdowns of the investor's holdings (weight > 0, in the given order); never interest-area,
    watchlist or other universe instruments (interest_tickers and drawdown_threshold are accepted for older
    callers and ignored). No holdings, no drawdown lines. Each line passes the briefing's advice filter or is
    dropped. No offer line (see attach_what_changed)."""
    from egoquant.briefing.loader import load_config
    names = names or {}
    advice = AdviceFilter(load_config().forbidden_patterns)
    lines = []
    order = {t: i for i, t in enumerate(holdings or [])}
    shown = sorted((a for a in drawdown_alerts if a.ticker in order), key=lambda a: order[a.ticker])
    for a in shown:
        name = names.get(a.ticker)
        who = f"{name} ({a.ticker})" if name and name != a.ticker else a.ticker
        to = f" (data to {data_to:%d %b %Y})" if data_to else ""
        lines.append(f"{who} closed {abs(a.drawdown_from_peak):.1%} below its highest close of the last "
                     f"{a.window_days} trading days{to}. "
                     + ("These are invented (synthetic) demo prices, not market data." if synthetic
                        else "This is past (in-sample) price data, not a forecast."))
    if briefing is not None:
        lines += [a.summary or a.message for a in briefing.alerts]     # levels, change, count, exposure
    return [line for line in lines if advice.is_clean(line)]


def attach_what_changed(report: ClientReport, drawdown_alerts: list[DrawdownAlert], briefing: Briefing | None,
                        names: dict[str, str] | None = None, data_to: date | None = None,
                        holdings: list[str] | None = None, interest_tickers: set[str] | None = None,
                        drawdown_threshold: float | None = None) -> ClientReport:
    """The "What changed" section (numbers unchanged). Its only action is offer_risk_profile_recheck, made when a
    holding's news tone FELL by the alert threshold or more (recheck_offered), or a holding of more than the
    alert exposure fell from its recent high by Agent C's drawdown threshold (drawdown_offered). Tone rises are
    listed without it; interest-area instruments never appear."""
    lines = what_changed_lines(drawdown_alerts, briefing, names, data_to, synthetic=report.data_source == "synthetic",
                               holdings=holdings, interest_tickers=interest_tickers,
                               drawdown_threshold=drawdown_threshold)
    held = set(holdings or [])
    weights = {r.asset_id: r.weight for r in report.allocation if r.asset_id in held}
    offer = bool(lines) and (recheck_offered(briefing)
                             or drawdown_offered(drawdown_alerts, weights, drawdown_threshold))
    return report.model_copy(update={"what_changed": lines,
                                     "what_changed_action": "offer_risk_profile_recheck" if offer else None})


def recheck_offered(briefing: Briefing | None) -> bool:
    """True when a holding's news tone fell enough to offer a risk-profile re-check (negative moves only)."""
    return briefing is not None and any(a.action == "offer_risk_profile_recheck" for a in briefing.alerts)


def drawdown_offered(drawdown_alerts: list[DrawdownAlert], weights: dict[str, float] | None,
                     drawdown_threshold: float | None = None) -> bool:
    """True when a HOLDING above the alert exposure (briefing config alerts.min_exposure, 5%) closed at least Agent C's
    drawdown threshold (10%) below its recent highest close. weights: holdings only, so interests never count.
    Without a threshold, Agent C's own alerts (already filtered by its threshold) count as they are."""
    from egoquant.briefing.loader import load_config
    min_exposure = load_config().alerts.min_exposure
    return any((weights or {}).get(a.ticker, 0.0) > min_exposure
               and (drawdown_threshold is None or abs(a.drawdown_from_peak) >= drawdown_threshold)
               for a in drawdown_alerts)


def what_changed_html(r: ClientReport) -> str:
    if not r.what_changed:
        return ""
    advice = AdviceFilter(_briefing_patterns())
    items = "".join(f"<li>{_e(advice.safe_line(x, SAFE_REPLACEMENT))}</li>"
                    for x in r.what_changed)
    offer = (f"<p class='recheck' data-action='{_e(r.what_changed_action)}'>{_e(RECHECK_OFFER)}</p>"
             if r.what_changed_action == "offer_risk_profile_recheck" else "")
    return f"<section><h2>What changed</h2><ul class='reasons'>{items}</ul>{offer}</section>"


def _briefing_patterns() -> list[str]:
    from egoquant.briefing.loader import load_config
    return load_config().forbidden_patterns


def category_basis(profile: RiskProfile | None) -> dict[str, float | int | str] | None:
    """How the risk category was set when the Risk Game was played (docs/RISK_GAME_BLEND.md); None otherwise, so a report without
    a game is exactly what it was. The numbers are the profile's own, the sentence is a fixed template."""
    if profile is None or not getattr(profile, "game_played", False):
        return None
    t, g, b, cap = profile.tolerance_level, profile.game_level, profile.blended_level, profile.capacity_level
    if profile.no_loss_gate:
        text = ("You said you could not accept your investment going down in value, so you are in the lowest category (C1) "
                "whatever the Risk Game said.")
    else:
        text = (f"Your category rests on three things: your answers on attitude to risk (level {t} of 5, two thirds), the Risk "
                f"Game you played (level {g} of 5, one third) and your finances (a cap at level {cap}). Your attitude to risk "
                f"came to level {b}; your category is the lower of that and the cap: {profile.c_level}.")
    return {"questionnaire_level": t, "game_level": g, "blended_level": b, "capacity_level": cap,
            "questionnaire_class": getattr(profile, "questionnaire_class", ""), "final": profile.c_level,
            "binding": profile.binding_dimension, "text": text}


def _with_profile(profile: RiskProfile | None, report: ClientReport) -> ClientReport:
    """Record when the risk profile was assessed and until when it is valid; flag an expired profile."""
    if profile is None:
        return report
    expired = is_expired(profile, report.as_of)
    update = {"profile_assessed_on": profile.assessed_at.date(), "profile_valid_until": profile.valid_until.date(),
              "profile_expired": expired}
    basis = category_basis(profile)
    if basis is not None:
        update["category_basis"] = basis
        update["reasons"] = [r for r in report.reasons if r.kind == "class"] + [Reason(kind="class", text=basis["text"])] + \
                            [r for r in report.reasons if r.kind != "class"]
    if expired:
        update["banners"] = [
            f"Your risk profile expired on {profile.valid_until:%d %b %Y}. This proposal still follows that "
            "assessment; nothing has been changed or blocked. Please re-take the risk questionnaire before "
            "relying on it. Would you like to re-assess now?", *report.banners]
    return report.model_copy(update=update)


def _interest_notes(selection: UniverseSelection, weights: dict[str, float], policy, config: AgentConfig,
                    names: dict[str, str]) -> list[str]:
    """One sentence per interest area: held, allowed but not bought (and why), or not allowed (and why)."""
    excluded = {e.ticker: e.reason.replace("the investor's", "your").replace("for this investor", "for you")
                for e in selection.excluded}
    caps = {"commodity": policy.max_commodity, "crypto": policy.max_crypto}
    method = ("lowest estimated risk" if config.objective == "min_variance"
              else "best balance of our return assumptions against risk")
    notes: list[str] = []
    for area in selection.interest_areas:
        tag = f"interest:{area}"
        allowed = [i for i in selection.instruments if tag in i.reasons and i.ticker not in selection.assets_to_exclude]
        blocked = [i for i in selection.watchlist if tag in i.reasons]
        label = area.replace("_", " ")
        held = [i for i in allowed if weights.get(i.ticker, 0.0) > 0.005]
        idle = [i for i in allowed if weights.get(i.ticker, 0.0) <= 0.005]
        parts: list[str] = []
        if held:
            parts.append("you hold " + _join([f"{names.get(i.ticker, i.ticker)} ({weights[i.ticker]:.0%})" for i in held]))
        if idle:
            cap_text = ""
            classes = {i.asset_class for i in idle}
            if len(classes) == 1 and next(iter(classes)) in caps:
                cap_text = f" (your category allows up to {caps[next(iter(classes))]:.0%})"
            shown = [names.get(i.ticker, i.ticker) for i in idle[:5]]     # the most relevant few; count the rest
            if len(idle) > 5:
                shown.append(f"{len(idle) - 5} more")
            parts.append(f"{_join(shown)} {'is' if len(idle) == 1 else 'are'} "
                         f"allowed{cap_text} but not bought: adding {'it' if len(idle) == 1 else 'them'} would not "
                         f"improve the {method} for your limits")
        if blocked:
            parts.append(_join([f"{names.get(i.ticker, i.ticker)} is not available to you "
                                f"({excluded.get(i.ticker, 'not allowed')})" for i in blocked]))
        if not (held or idle or blocked):
            parts.append(_nothing_offered(selection, area))
        notes.append(f"You asked about {label}: " + "; ".join(parts) + ".")
    return notes


def _nothing_offered(selection: UniverseSelection, area: str) -> str:
    """Why an interest area has nothing in the portfolio: prices missing this run, the platform, or no product."""
    candidates = set(selection.queues.get(area, []))
    unpriced = [e for e in selection.not_fetched if e.ticker in candidates]
    if unpriced or candidates:                  # products exist for it, but none could be priced this time
        why = f" ({unpriced[0].reason})" if unpriced else ""
        return (f"we could not load prices for its investments this time{why}, so none were included; "
                "rebuilding later may add them")
    if selection.trading_platforms:
        return "nothing in our product list for it can be bought on your trading platform"
    return "nothing in our product list matches it"


def _mix_phrase(split: dict[str, float]) -> str:
    """'40% in stocks, 45% in bonds, 10% in gold & commodities and 5% in cash' (stocks, bonds and cash
    always; gold and crypto only when held)."""
    parts = [f"{split[c]:.0%} in {CLASS_LABEL[c].lower()}" for c in ("equity", "bond", "commodity", "crypto", "cash")
             if split[c] > 0.005 or c in ("equity", "bond", "cash")]
    return _join(parts)


def _holding_cap_source(context) -> str:
    """Where the largest-holding limit comes from: the standard limit unless the request set its own."""
    if abs(context.max_asset_weight - type(context).model_fields["max_asset_weight"].default) < 1e-9:
        return f"the standard limit of {context.max_asset_weight:.0%} on any one holding"
    return "your holding-size preference"


def _check(name: str, value: float, limit: float, direction: str, source: str) -> LimitCheck:
    ok = value <= limit + 1e-6 if direction == "max" else value >= limit - 1e-6
    return LimitCheck(name=name, value=value, limit=limit, direction=direction, ok=ok, source=source)


# --------------------------------------------------------------------------
# Goal section: the simulation in the investor's terms (see the module docstring for the research)
# --------------------------------------------------------------------------
GOAL_DISCLOSURES = [
    "IMPORTANT: The projections or other information in this section regarding the likelihood of various "
    "investment outcomes are hypothetical in nature, do not reflect actual investment results and are not "
    "guarantees of future results.",
    "Results may vary with each use and over time: new market data, a change to your goal, your monthly amount, "
    "your time horizon or the funds available can all change them.",
    "The simulation replays past market behaviour. The future can be worse than anything in the history used, "
    "and the simulation cannot show events that history does not contain.",
    "Assumed: the monthly amount is paid in (or taken out) every month as planned, the portfolio is rebalanced "
    "back to its mix every month, and today's fund fees apply. Not included: taxes, inflation (amounts are in "
    "future dollars), trading costs after the first purchase, and changes you make along the way.",
    "A high chance is not a promise and a low chance is not a certainty of failure. Review the plan when your "
    "situation or the markets change.",
]
GOAL_LEVERS_INTRO = ("Each line changes ONE thing and re-runs the same simulated futures with the same portfolio, "
                     "so the difference comes only from that change.")


def _round_money(x: float) -> float:
    """Three significant figures (283,000 not 282,779): the simulation cannot support more precision."""
    if abs(x) < 1000:
        return float(round(x / 10) * 10)
    digits = len(str(int(abs(x)))) - 3
    return float(round(x, -digits))


def _chance(p: float) -> str:
    """A chance as shown: whole percent, never a misleading 0% or 100% for a near miss."""
    if p <= 0:
        return "0%"
    if p >= 1:
        return "100%"
    if p < 0.005:
        return "less than 1%"
    if p > 0.995:
        return "more than 99%"
    return f"{p:.0%}"


def _in_100(p: float) -> int:
    """Icons filled out of 100: never 0 or 100 unless every future misses or reaches the goal."""
    if p <= 0:
        return 0
    if p >= 1:
        return 100
    return min(max(int(round(p * 100)), 1), 99)


def _years(h: float) -> str:
    return f"{h:g} year" + ("" if h == 1 else "s")


def _regime_step(rec: dict | None) -> GoalStep | None:
    """Only when the regime-aware sampler ran (egoquant/regime, EGOQUANT_REGIME_MC): the model in plain words,
    today's regime and how often the simulated futures hold a long crisis, both taken from the simulation itself."""
    from egoquant.regime.hook import is_regime
    if not is_regime(rec):
        return None
    share = rec.get("long_crisis_share")
    if share is None:
        crisis = ""
    elif share >= 1:
        crisis = " In every simulated future, there is at least one crisis period lasting 3 months or more."
    elif share <= 0:
        crisis = " In none of the simulated futures is there a crisis period lasting 3 months or more."
    else:
        crisis = (f" In about {_in_100(share)} of every 100 simulated futures, there is at least one crisis period "
                  f"lasting 3 months or more.")
    return GoalStep(
        title="Market regimes",
        text=f"A machine-learning model (a Hidden Markov Model fitted on the daily returns and volatility of "
             f"{_regime_funds(rec)}, {str(rec.get('fit_start'))[:4]} to {str(rec.get('fit_end'))[:4]}) sorts every past "
             f"trading day into one of {_regime_count(rec)} market regimes: {_regime_names(rec)}. Each simulated future "
             f"moves from regime to "
             f"regime month by month with the chances the model learned, and each month is drawn only from past months "
             f"that began in the same regime, so a crisis lasts as long as crises really did instead of being broken up. "
             f"Market regime today: {rec.get('regime_today')} (probability "
             f"{_chance(float(rec.get('regime_today_probability') or 0.0))})." + crisis)


_FUND_WORDS = {"SPY": "the S&P 500 fund SPY", "AGG": "the US bond fund AGG"}
_COUNT_WORDS = {2: "two", 3: "three", 4: "four"}


def _regime_funds(rec: dict) -> str:
    funds = [_FUND_WORDS.get(t, t) for t in (rec.get("model_funds") or ["SPY"])]
    return funds[0] if len(funds) == 1 else ", ".join(funds[:-1]) + " and " + funds[-1]


def _regime_names(rec: dict) -> str:
    names = list(rec.get("state_names") or ["calm", "stressed", "crisis"])
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _regime_count(rec: dict) -> str:
    n = len(rec.get("state_names") or ["calm", "stressed", "crisis"])
    return _COUNT_WORDS.get(n, str(n))


def build_goal_report(g: GoalAnalysis, result: AgentAResult, screening, currency: str, tie: float = 0.01) -> GoalReport:
    """Every number and sentence of the goal section, from the simulation (deterministic templates)."""
    spec, n = g.spec, g.n_paths
    o = next(x for x in g.outcomes if x.menu_index == g.chosen_index)
    m = lambda x: _money(_round_money(x), currency)                     # noqa: E731
    exact = lambda x: _money(x, currency)                               # noqa: E731
    k, horizon = _in_100(o.prob_goal), _years(spec.horizon_years)
    goal_text = exact(spec.goal_amount)

    if o.prob_goal <= 0:
        headline = (f"In none of the {n:,} simulated futures does this portfolio reach your goal of {goal_text} "
                    f"in {horizon}. The goal looks out of reach with this plan; \"What would change your odds\" shows what "
                    f"would help.")
    elif o.prob_goal >= 1:
        headline = f"In every one of the {n:,} simulated futures, this portfolio reaches your goal of {goal_text} in {horizon}."
    else:
        headline = (f"In about {k} of every 100 simulated futures, this portfolio reaches your goal of {goal_text} "
                    f"or more in {horizon}. In the other {100 - k} it falls short.")

    flow = spec.monthly_flow
    flow_text = (f"add {exact(flow)} every month" if flow > 0 else f"take out {exact(-flow)} every month" if flow < 0
                 else "add nothing more")
    plan = (f"You start with {exact(spec.start_amount)}, {flow_text}, and want {goal_text} in {horizon}. "
            f"Paid in without any growth: {exact(g.total_contributed)}.")

    pts = max(1, round(1.96 * g.std_error * 100))                      # 95% sampling error, whole points
    if 0 < o.prob_goal < 1:
        precision = (f"With {n:,} simulated futures the simulation's own error is about ±{pts} percentage "
                     f"point{'s' if pts > 1 else ''}, so the chance is shown rounded.")
    else:
        precision = f"{'None' if o.prob_goal <= 0 else 'Every one'} of the {n:,} simulated futures reached the goal."
    if len(g.period_checks) == 2:
        a, b = g.period_checks
        halves = (f"drawing only from {a.start[:4]} to {a.end[:4]} gives {_chance(a.prob_goal)}, only from "
                  f"{b.start[:4]} to {b.end[:4]} gives {_chance(b.prob_goal)}")
        if 0 < o.prob_goal < 1:
            precision += (f" Which years history happens to contain matters far more: {halves}. Read the chance as a "
                          f"rough guide, not an exact figure.")
        else:
            precision += f" Each half of the history on its own: {halves}."

    outcomes = [
        GoalRow(label="1 in 10 futures ends below", value=m(o.p10),
                text=f"In the worst 1 in 10 simulated futures you would end with less than {m(o.p10)}."),
        GoalRow(label="Half end above", value=m(o.p50),
                text=f"Half of the simulated futures end above {m(o.p50)} and half below. This middle outcome is "
                     f"not a promise, and not more likely than the amounts around it."),
        GoalRow(label="1 in 10 ends above", value=m(o.p90),
                text=f"In the best 1 in 10 simulated futures you would end with more than {m(o.p90)}."),
    ]

    severity = []
    if o.worst5_shortfall > 0.5:
        severity.append(GoalRow(label="If it goes badly", value=m(o.worst5_shortfall) + " short",
                                text=f"In the worst 5 of every 100 simulated futures you would end, on average, "
                                     f"{m(o.worst5_shortfall)} short of your goal."))
    else:
        severity.append(GoalRow(label="If it goes badly", value="Goal still reached",
                                text="Even in the worst 5 of every 100 simulated futures you would reach your goal."))
    dd20 = _in_100(o.prob_drawdown_20)
    if o.drawdown_p50 < 0.005:
        fall = ("Along the way, the account hardly falls: less than 1% below its previous high in a typical future"
                + (f", {o.drawdown_p90:.0%} or more in 1 in 10. " if o.drawdown_p90 >= 0.005 else ". "))
    else:
        fall = (f"Along the way, the account falls at some point by about {o.drawdown_p50:.0%} from its previous high "
                f"in a typical future, and by {o.drawdown_p90:.0%} or more in 1 in 10. ")
    severity.append(GoalRow(
        label="Falls along the way",
        value=f"-{o.drawdown_p50:.0%} typical" if o.drawdown_p50 >= 0.005 else "Under 1% typical",
        text=fall
             + (f"About {dd20} in 100 futures see a fall of 20% or more." if o.prob_drawdown_20 > 0
                else "None of the simulated futures sees a fall of 20% or more.")))
    if flow < 0 or o.prob_depleted > 0:
        runs_out = _in_100(o.prob_depleted)
        severity.append(GoalRow(
            label="Money runs out", value=f"{runs_out} in 100" if o.prob_depleted > 0 else "Never",
            text=(f"In about {runs_out} of every 100 simulated futures the monthly withdrawals use up all the money "
                  f"before the {horizon} are over." if o.prob_depleted > 0
                  else "In none of the simulated futures do the monthly withdrawals use up all the money.")))

    levers = []
    for lv in g.levers:
        if lv.kind == "monthly":
            new = (f"add {exact(lv.value)} a month" if lv.value > 0 else f"take out {exact(-lv.value)} a month"
                   if lv.value < 0 else "stop the monthly withdrawals")
            old = (f"{exact(flow)}" if flow > 0 else f"taking out {exact(-flow)}" if flow < 0 else "nothing")
            change = f"{new[0].upper()}{new[1:]} instead of {old}"
            levers.append(GoalLever(kind="monthly", change=change, chance=_chance(lv.prob_goal), prob_goal=lv.prob_goal))
        elif lv.kind == "horizon":
            extra = lv.value - spec.horizon_years
            levers.append(GoalLever(kind="horizon", change=f"Give it {_years(extra)} more ({_years(lv.value)} in total)",
                                    chance=_chance(lv.prob_goal), prob_goal=lv.prob_goal))
        elif lv.kind == "goal":
            levers.append(GoalLever(kind="goal", change=f"Aim for {exact(lv.value)} instead of {goal_text}",
                                    chance=_chance(lv.prob_goal), prob_goal=lv.prob_goal))

    span_years = (date.fromisoformat(g.history_end) - date.fromisoformat(g.history_start)).days / 365.25
    events = [label for start, end, label in (("2008-09-15", "2009-03-09", "the 2008-09 financial crisis"),
                                              ("2020-02-19", "2020-03-23", "the 2020 COVID crash"),
                                              ("2022-01-03", "2022-10-12", "the 2022 fall of stocks and bonds together"))
              if g.history_start <= start and g.history_end >= end]
    held = sum(1 for w in result.final.weights.values() if w > 1e-9)
    source = "synthetic (invented)" if result.final.data_source == "synthetic" else "real"
    method = [
        GoalStep(title="Market history",
                 text=f"We took the {source} daily price changes of the {held} investments in this portfolio from "
                      f"{g.history_start} to {g.history_end} (about {span_years:.0f} years, {g.history_days:,} trading "
                      f"days)" + (f", a period that includes {_join(events)}." if events and source == "real" else ".")),
        GoalStep(title=f"{n:,} possible futures",
                 text=f"Each simulated future is built month by month: we pick a random stretch of {g.block_days} "
                      f"consecutive trading days (about one month) from that history, then another, and so on for "
                      f"{horizon}. Whole months keep crashes and rebounds together, the way they really happened."),
        GoalStep(title="Your plan in each future",
                 text=f"Each month the portfolio earns that month's returns (rebalanced back to its mix), pays its fund "
                      f"fees, and then you {flow_text.replace(' every month', '')}. Money cannot go below zero."),
        GoalStep(title="Counting the futures",
                 text=f"We count the futures that end at or above {goal_text}: {round(o.prob_goal * n):,} of {n:,}, "
                      f"which is {_chance(o.prob_goal)}."),
    ]
    regime_step = _regime_step(g.sampler)
    if regime_step is not None:                                         # right after "possible futures"
        method.insert(2, regime_step)
    corrections = []
    if g.mean_adjustments:
        corrections.append(f"Stocks, stock funds and crypto-assets ({len(g.mean_adjustments)} in this simulation) are "
                           "picked from today's most traded list, so their past returns flatter them (survivorship "
                           "bias). We kept their real ups and downs but set their average return to what their "
                           "sensitivity to the world stock market earned (beta times the market's average).")
    if g.backfilled:
        corrections.append(f"Investments younger than the history used ({len(g.backfilled)} in this simulation) had "
                           "their missing early years rebuilt from the long-history funds they move with, adding no "
                           "extra return.")
    if corrections:
        method.append(GoalStep(title="Corrections against over-optimism", text=" ".join(corrections)))
    method.append(GoalStep(title="The same futures for every option",
                           text=f"Every shortlisted portfolio was tested on exactly the same {n:,} futures (fixed random "
                                f"seed {g.seed}), so differences come from the portfolios, not from luck, and the same "
                                f"inputs always give the same result."))

    menu = result.menu.portfolios
    approved = len(screening.approved) if screening is not None else len(g.outcomes)
    best = max(x.prob_goal for x in g.outcomes)
    close = [x for x in g.outcomes if x.prob_goal >= best - tie - 1e-12]
    if best <= 0:
        pick = ("None of them reached the goal in any simulated future, so we chose the one that falls least short "
                "in the worst 5 of every 100 futures.")
    elif len(close) > 1:
        pick = (f"The best chance among them was {_chance(best)}. {len(close)} portfolios were within {tie * 100:g} percentage point{'' if tie == 0.01 else 's'} of it, "
                f"too close for the simulation to tell apart, so we chose the one of those that falls least short in "
                f"the worst 5 of every 100 futures ({_chance(o.prob_goal)} chance).")
    else:
        pick = f"This portfolio had the highest chance of reaching your goal: {_chance(o.prob_goal)}."
    choice = [f"We built {len(menu)} different portfolios, every one inside your limits.",
              f"The behaviour check kept the {approved} that fit how you take risk.",
              f"Each of those {approved} was simulated against your goal on the same futures. {pick}"]
    shortlist = [GoalShortlistRow(label=menu[x.menu_index].message.split(" | ")[0], equity_weight=x.equity_weight,
                                  prob_goal=x.prob_goal, p50=x.p50, worst5_shortfall=x.worst5_shortfall,
                                  chosen=x.menu_index == g.chosen_index) for x in g.outcomes]

    return GoalReport(
        label=g.label, goal_amount=spec.goal_amount, start_amount=spec.start_amount, monthly_flow=flow,
        horizon_years=spec.horizon_years, total_contributed=g.total_contributed, prob_goal=o.prob_goal, in_100=k,
        chance=_chance(o.prob_goal), headline=headline, plan=plan, precision=precision, outcomes=outcomes,
        severity=severity, fan=list(g.fan), levers_intro=GOAL_LEVERS_INTRO, levers=levers, method=method,
        choice=choice, shortlist=shortlist, period_checks=list(g.period_checks), std_error=g.std_error, n_paths=n,
        history_start=g.history_start, history_end=g.history_end, disclosures=list(GOAL_DISCLOSURES),
    )


def _banners(data_source: str, config: AgentConfig, risk_review: RiskReview | None = None) -> list[str]:
    out = []
    if risk_review is not None and not risk_review.approved:
        if risk_review.violations and all(v.startswith("proposal has no holdings") for v in risk_review.violations):
            out.append("NO PORTFOLIO: no portfolio fits all of your limits at once. A high cash or liquidity need is the usual "
                       "cause: try a lower percentage, or choose more interest areas.")
        else:
            out.append("NOT APPROVED: the independent risk check rejected this portfolio, so it must not be offered. "
                       "Problems found: " + "; ".join(plain_violations(list(risk_review.violations))))
    if data_source == "synthetic":
        out.append("DEMO: this report uses synthetic (invented) market data. It is not a real recommendation.")
    if config.risk_class_policies == DEFAULT_RISK_CLASS_POLICIES:
        out.append("DRAFT LIMITS: the C1-C5 limits are EgoQuant's provisional placeholder defaults; a bank would replace "
                   "them with its own approved mapping.")
    return out


def _disclosures(config: AgentConfig) -> list[str]:
    return [
        "Past data does not predict the future. Volatility and drawdown figures are estimates from recent history and can be wrong.",
        "Stress scenarios are hypothetical shocks chosen in advance. They are not forecasts, and real losses can be larger.",
        "This engine makes no forecast of returns. It is designed to control risk within your limits, not to promise gains.",
        "Fund fees are shown as annual percentages. Trading costs are estimates and apply only to the first purchase or to changes.",
        f"Risk estimates use {ESTIMATOR_LABELS.get(config.cov_estimator, config.cov_estimator)} over about {config.window_days} trading days.",
    ]


# --------------------------------------------------------------------------
# Reasons: fixed sentence templates, filled with numbers from the result
# --------------------------------------------------------------------------
def _reasons(result: AgentAResult, context: InvestorContext, config: AgentConfig, policy, split: dict[str, float],
             scenarios: dict[str, StressScenario], names: dict[str, str],
             assets: list[AssetInfo] | None = None, screening=None) -> list[Reason]:
    final = result.final
    reasons: list[Reason] = []
    scen = scenarios.get(config.capacity_scenario)
    scen_text = scen.description.lower() if scen else "a severe market fall"

    reasons.append(Reason(kind="class", text=(
        f"Your answers placed you in category {context.risk_class} (C1 is the most cautious, C5 the most growth-oriented). "
        f"That category aims for about {_pct(policy.target_equity)} in stocks and sets hard limits of at most {_pct(policy.max_equity)} in stocks, "
        f"{_pct(policy.max_commodity)} in gold and commodities, {_pct(policy.max_crypto)} in crypto, "
        f"{_pct(policy.max_vol)} volatility and a {_pct(policy.max_stress_loss)} loss in one specific test: {scen_text}.")))

    goal_based = final.method.startswith("goal_based")
    d = final.diagnostics
    v2_used = "v2_effective_n" in d                  # portfolio method v2 (docs/PORTFOLIO_V2.md)
    if v2_used and goal_based:
        method = (f"We built {int(float(d.get('goal_candidates', 0)))} different portfolios inside your limits with several "
                  f"established methods ({v2_method_names()}). The behaviour check kept {int(float(d.get('goal_approved', 0)))} that fit how you take risk. "
                  f"The best of them had a {float(d.get('goal_probability', 0)):.0%} chance of reaching your goal in a simulation on "
                  f"past market data; among the {int(float(d.get('v2_n_contenders', 1)))} whose chance was within "
                  f"{float(d.get('v2_tolerance_points', 1)):.0f} points of that, we chose the one with the smallest fall from a peak "
                  f"in the worst 10% of simulated futures ({float(d.get('v2_worst10_drawdown', 0)):.0%}). "
                  "Those chances are hypothetical, not a forecast.")
    elif v2_used:
        method = (f"Within your limits we built portfolios with several established methods ({v2_method_names()}) and chose "
                  "the one with the best balance between assumed return and risk among those spread over enough funds. "
                  "We do not forecast returns.")
    elif goal_based:
        method = (f"We built {int(float(d.get('goal_candidates', 0)))} different portfolios inside your limits. The behaviour check kept "
                  f"{int(float(d.get('goal_approved', 0)))} that fit how you take risk, and we chose the one with the highest "
                  f"chance of reaching your goal ({float(d.get('goal_probability', 0)):.0%}) in a simulation on past market "
                  "data. That chance is hypothetical, not a forecast.")
    elif config.objective == "mean_variance":
        method = ("Within your limits we choose the mix of funds that balances our return assumptions against estimated volatility.")
    else:
        method = ("Within your limits we choose the mix of funds with the lowest estimated volatility for the chosen stock level. "
                  "We do not forecast returns, so the portfolio is built to control risk rather than to chase gains.")
    reasons.append(Reason(kind="method", text=method))
    if goal_based and screening is not None:
        reasons.extend(_goal_behaviour_reasons(screening, policy, split, final))

    # constraints that actually held the answer back (binding), in plain words
    values = _constraint_values(result, context, policy, final)
    values.update({f"system:instrument_cap[{a.asset_id}]": a.max_weight for a in assets or [] if a.max_weight})
    # One sentence per limit. A per-asset limit can bind on several assets (labels end in [ASSET_ID]); name them all.
    groups: dict[str, list[str]] = {}
    for label in final.binding_constraints:
        groups.setdefault(label if label.startswith("behavioral:") or "max_stress_loss" in label or "max_class_weight" in label else label.split("[")[0], []).append(label)
    seen_equity_caps: set[float] = set()
    most_important_said = False
    for key, labels in groups.items():
        value = values.get(labels[0], values.get(labels[0].split("[")[0]))
        if key in ("risk_class:max_equity", "suitability:max_equity") and value is not None:
            if round(value, 6) in seen_equity_caps:             # the same cap from two sources: say it once
                continue
            seen_equity_caps.add(round(value, 6))
        text = _binding_text(labels[0], value, result)
        if not text:
            continue
        if text.startswith("The most important limit was"):    # only the first of them is "the most important"
            if most_important_said:
                text = "A limit that shaped the mix: " + text[len("The most important limit was"):].strip().capitalize()
            most_important_said = True
        if key in ("investor_preference:max_asset_weight", "system:instrument_cap"):
            held = [names.get(l[l.index("[") + 1:-1], l[l.index("[") + 1:-1]) for l in labels if "[" in l]
            if held:
                text = text.rstrip(".") + f" ({' and '.join(held) if len(held) < 3 else ', '.join(held)} reached it)."
        reasons.append(Reason(kind="behavioral" if key.startswith("behavioral:") else "limit", text=text))

    for label in final.ignored_constraints:
        c = next((c for c in result.behavioral_constraints if c.label == label), None)
        conf = f" (confidence {c.confidence:.0%})" if c and c.confidence is not None else ""
        reasons.append(Reason(kind="ignored", text=(
            f"We did not apply the behavioural signal '{_plain_label(label)}'{conf} because the evidence was weaker than our "
            f"{config.min_behavioral_confidence:.0%} confidence threshold.")))
    for label in final.relaxed_constraints:
        reasons.append(Reason(kind="relaxed", text=(
            f"To find any portfolio we had to relax the soft preference '{_plain_label(label)}'. Your hard limits were not relaxed.")))

    # equity lowered below its starting level?
    usable_caps = [c.value for c in result.behavioral_constraints
                   if c.type == "max_equity" and c.label not in final.ignored_constraints]
    start = min([policy.target_equity, context.suitability_max_equity, policy.max_equity] + usable_caps)
    if split["equity"] < start - 0.005 and not goal_based:   # goal path: the stock share comes from the goal choice
        phrases = [_limit_phrase(label, values.get(label, values.get(label.split("[")[0])), result)
                   for label in final.limiting_constraints
                   if not label.startswith("risk_class:max_class_weight")]       # a cap on cash, bonds, gold or crypto cannot stop MORE stocks
        phrases = [p for p in phrases if p]
        if phrases:
            behavioral_limit = any(label.startswith("behavioral:") for label in final.limiting_constraints)
            reasons.append(Reason(kind="behavioral" if behavioral_limit else "limit", text=(
                f"We lowered the share in stocks from {start:.0%} to {split['equity']:.0%} because a higher share "
                f"would break {_join(phrases)}.")))
        else:
            reasons.append(Reason(kind="limit", text=(
                f"We lowered the share in stocks from {start:.0%} to {split['equity']:.0%} because your limits could not all be met at the higher level.")))

    d = final.diagnostics
    ratio, scale = float(d.get("regime_ratio", 1.0)), float(d.get("risk_budget_scale", 1.0))
    if scale < 1.0:
        reasons.append(Reason(kind="regime", text=(
            f"Markets are currently more volatile than usual (recent volatility is {ratio:.2f} times its long-run level), "
            f"so we cut the volatility budget to {scale:.0%} of normal.")))
    else:
        reasons.append(Reason(kind="regime", text=(
            f"Market volatility is currently {d.get('regime_label', 'normal')} (recent volatility is {ratio:.2f} times its long-run level), "
            f"so no extra reduction of the risk budget was needed.")))

    n_funds = sum(1 for w in final.weights.values() if w > 0.005)
    reasons.append(Reason(kind="diversification", text=(
        f"The portfolio holds {n_funds} funds, equivalent to about {final.risk.effective_n_holdings:.1f} equally sized holdings. "
        f"Its volatility is {_lower_by(final.risk.diversification_ratio)} lower than if every holding moved in step with the others.")))
    if "v2_effective_n" in d:
        caps = ("No single fund is above 30% of the account" if float(d.get("v2_largest", 1.0)) <= 0.30 + 1e-6
                else f"The largest fund is {float(d.get('v2_largest', 0)):.0%} of the account")
        if float(d.get("v2_group_caps_dropped", 0.0)):
            group = "; the funds available to you were too similar for the limit on near-identical funds (25%) to apply, so it was relaxed"
        else:
            group = "; funds that move almost identically are limited to 25% together"
        reasons.append(Reason(kind="screening", text=(
            f"We started from {int(float(d.get('v2_n_universe', 0)))} products you are eligible for, grouped those that behave alike "
            f"and compared {int(float(d.get('v2_n_representatives', 0)))} representatives. {caps}{group}.")))

    if final.sensitivity:
        worst = max(r.max_abs_weight_change for r in final.sensitivity)
        reasons.append(Reason(kind="robustness", text=(
            f"Robustness check: re-running with different history lengths and estimation methods, no holding moved by more than "
            f"{worst * 100:.1f} percentage points.")))
    return reasons


def _constraint_values(result: AgentAResult, context: InvestorContext, policy, final) -> dict[str, float]:
    """Numeric value of each constraint label, so reason sentences can quote it."""
    v = {"risk_class:max_equity": policy.max_equity, "risk_class:max_vol": policy.max_vol,
         "risk_class:max_stress_loss": policy.max_stress_loss, "suitability:max_equity": context.suitability_max_equity,
         "investor_preference:max_asset_weight": context.max_asset_weight,
         "suitability:min_cash": max(context.min_cash_buffer, context.liquidity_need)}
    if context.max_acceptable_loss is not None:
        v["suitability:max_stress_loss"] = context.max_acceptable_loss
    if "risk_budget_vol" in final.diagnostics:
        v["system:max_vol"] = float(final.diagnostics["risk_budget_vol"])
    if context.max_turnover is not None:
        v["investor_preference:max_turnover"] = context.max_turnover
    min_cash = max(context.min_cash_buffer, context.liquidity_need)
    for asset_class, field_name in CLASS_CAP_FIELDS.items():
        cap = getattr(policy, field_name)
        v[f"risk_class:max_class_weight[{asset_class}]"] = max(cap, min_cash) if asset_class == "cash" else cap
    for c in result.behavioral_constraints:
        v[c.label] = c.value
    return {**v}


def _goal_behaviour_reasons(screening, policy, split: dict[str, float], final) -> list[Reason]:
    """On the goal path the stock share comes from the goal choice, so the 'we lowered the share' sentence is skipped. When
    Agent B's loss limit (the investor's own trading history, crash test or game) is TIGHTER than the category's, the portfolios
    considered all stay inside it: say so, otherwise a much lower stock share than the category aims for is unexplained."""
    for c in getattr(screening, "constraints", None) or []:
        if c.type != "max_stress_loss" or c.source != "behavioral" or c.label in final.ignored_constraints:
            continue
        if c.value >= policy.max_stress_loss - 1e-9:
            continue
        conf = f" (confidence {c.confidence:.0%})" if c.confidence is not None else ""
        text = (f"Your trading history suggests you are more comfortable with a loss of at most {_pct(c.value)} in a severe market "
                f"fall{conf}, tighter than the {_pct(policy.max_stress_loss)} your category allows, so every portfolio we considered "
                "stays inside it.")
        if split["equity"] < policy.target_equity - 0.005:
            text += (f" That is why the portfolio holds {_pct(split['equity'])} in stocks, below the "
                     f"{_pct(policy.target_equity)} your category aims for.")
        return [Reason(kind="behavioral", text=text)]
    return []


def _pct(value: float) -> str:
    """40% for 0.4 and 12.5% for 0.125 (one decimal only when the number is not whole; never banker's rounding of a half)."""
    shown = f"{value * 100:.1f}"
    return (shown[:-2] if shown.endswith(".0") else shown) + "%"


def _lower_by(diversification_ratio: float) -> str:
    """'6%' for a diversification ratio of 1.06: how much lower the portfolio's volatility is than the weighted average."""
    ratio = float(diversification_ratio)
    return f"{(1.0 - 1.0 / ratio) * 100:.0f}%" if ratio > 1.0 else "0%"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _limit_phrase(label: str, value: float | None, result: AgentAResult) -> str | None:
    """A limit as a noun phrase ("your class volatility cap of 10%"), for the 'lowered' sentence."""
    if value is None:
        return None
    key = label.split("[")[0]
    if key == "risk_class:max_class_weight":
        return f"your category's cap of {_pct(value)} in {_class_of(label)}"
    templates = {
        "risk_class:max_vol": f"your category's volatility cap of {_pct(value)}",
        "risk_class:max_stress_loss": f"your category's cap of {_pct(value)} loss in a severe market fall",
        "suitability:max_stress_loss": f"the {_pct(value)} loss you said you can afford in a severe market fall",
        "suitability:min_cash": f"the {_pct(value)} you need to keep in cash",
        "investor_preference:max_asset_weight": f"the {_pct(value)} cap on any single holding",
        "investor_preference:max_turnover": f"the limit of trading at most {_pct(value)} of the portfolio",
        "system:max_vol": f"the {value:.1%} volatility budget set by current market conditions",
    }
    if key in templates:
        return templates[key]
    if key.startswith("behavioral:"):
        c = next((c for c in result.behavioral_constraints if c.label == label), None)
        conf = f", confidence {c.confidence:.0%}" if c and c.confidence is not None else ""
        what = {"max_equity": f"at most {_pct(value)} in stocks",
                "max_stress_loss": f"a loss of at most {_pct(value)} in a severe market fall",
                "max_turnover": f"trading at most {_pct(value)} of the portfolio"}.get(key.split(":")[1], f"a limit of {value:.2f}")
        return f"the behavioural limit of {what} (based on your past choices{conf})"
    return None


def _class_of(label: str) -> str:
    """'risk_class:max_class_weight[commodity]' -> 'gold & commodities'."""
    asset_class = label[label.index("[") + 1:label.index("]")]
    return CLASS_LABEL.get(asset_class, asset_class).lower()


def _plain_label(label: str) -> str:
    source, _, rest = label.partition(":")
    kind = rest.split("[")[0].replace("_", " ")
    return f"{source.replace('_', ' ')}: {kind}"


def _binding_text(label: str, value: float | None, result: AgentAResult) -> str | None:
    """One sentence for a binding constraint. The key lookup ignores the [scenario] suffix."""
    key = label.split("[")[0]
    val = value
    if val is None:
        return None
    if key == "risk_class:max_class_weight":
        return f"A limit that shaped the mix: your category allows at most {_pct(val)} in {_class_of(label)}."
    templates = {
        "risk_class:max_equity": f"The most important limit was your category's cap of {_pct(val)} in stocks.",
        "suitability:max_equity": f"The most important limit was the cap of {_pct(val)} in stocks set by your finances and answers.",
        "risk_class:max_vol": f"The most important limit was your category's volatility cap of {_pct(val)}.",
        "risk_class:max_stress_loss": f"The most important limit was your category's cap of {_pct(val)} loss in a severe market fall.",
        "suitability:max_stress_loss": f"The most important limit was the {_pct(val)} loss you said you can afford.",
        "suitability:min_cash": f"A limit that shaped the mix: you need at least {val:.0%} in cash for liquidity.",
        "investor_preference:max_asset_weight": f"A limit that shaped the mix: no single holding above {val:.0%}.",
        "investor_preference:max_turnover": f"A limit that shaped the mix: trade no more than {val:.0%} of the portfolio.",
        "system:max_vol": f"The volatility budget set by current market conditions ({val:.1%}) was binding.",
        "system:instrument_cap": f"A limit that shaped the mix: no single stock above {val:.0%} of the portfolio.",
    }
    if key in templates:
        return templates[key]
    if key.startswith("behavioral:"):
        c = next((c for c in result.behavioral_constraints if c.label == label), None)
        conf = f", confidence {c.confidence:.0%}" if c and c.confidence is not None else ""
        ref = f", evidence {c.evidence_ref}" if c and c.evidence_ref else ""
        what = {"max_equity": f"at most {val:.0%} in stocks", "max_vol": f"volatility of at most {val:.0%}",
                "max_stress_loss": f"a loss of at most {val:.0%} in a severe fall", "min_cash": f"at least {val:.0%} in cash",
                "max_asset_weight": f"no holding above {val:.0%}"}.get(key.split(":")[1], f"limit {val:.2f}")
        return (f"Your past choices suggest you are more comfortable with {what}. "
                f"This behavioural signal (source: behavioural analysis{conf}{ref}) was the limit holding the portfolio back.")
    return None


# ==========================================================================
# HTML rendering (inline SVG, no external libraries)
# ==========================================================================
_CSS = """
:root{color-scheme:light;--page:#ffffff;--surface:#ffffff;--ink:#18181b;--ink2:#52525b;--ink3:#a1a1aa;--line:#e4e4e7;
--grid:#f4f4f5;--gray:#d4d4d8;--s1:#18181b;--s2:#71717a;--s3:#e4e4e7;--s4:#d4d4d8;--s5:#3f3f46;--ok:#18181b;--warn:#18181b;
--bad:#dc2626;--banner:#fafafa;--bannerink:#18181b;--serif:"Instrument Serif",Georgia,"Times New Roman",serif;
--sans:"DM Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
main{max-width:880px;margin:0 auto;padding:40px 16px 64px}
h1{font:400 44px/1.05 var(--serif);margin:0 0 12px;padding-bottom:16px;border-bottom:2px solid var(--ink)}
h2{font:400 28px/1.15 var(--serif);margin:0 0 8px}
.sub{color:var(--ink2);margin:0 0 8px;font-size:13px}.lede{font:400 22px/1.3 var(--serif);margin:12px 0 0}
section{background:var(--surface);border-top:2px solid var(--ink);padding:24px 0 8px;margin:32px 0 0}
.banner{background:var(--banner);color:var(--bannerink);border:1px solid var(--line);border-left:2px solid var(--ink);padding:10px 14px;
margin:8px 0;font-weight:600;font-size:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:0;margin-top:16px;border-top:1px solid var(--line);border-left:1px solid var(--line)}
.tile{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:14px 16px}
.tile .v{font:400 34px/1.05 var(--serif)}.tile .k{color:var(--ink3);font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:12px}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
th{color:var(--ink3);font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;border-bottom:2px solid var(--ink)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tw,.sv{overflow-x:auto}@media (max-width:700px){svg.wide{min-width:600px}}
summary{cursor:pointer;margin-top:12px;font-size:12px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--ink2)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--ink2);font-size:13px;margin:10px 0}
.sw{display:inline-block;width:10px;height:10px;margin-right:6px;vertical-align:baseline;outline:1px solid var(--line)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}
svg{width:100%;height:auto;display:block}svg text{fill:var(--ink2);font-size:12px;font-family:var(--sans)}svg .v{fill:var(--ink);font-weight:600}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--line);stroke-width:1}
.c-equity{fill:var(--s1)}.c-bond{fill:var(--s2)}.c-cash{fill:var(--s3)}.c-commodity{fill:var(--s4)}.c-crypto{fill:var(--s5)}.c-gray{fill:var(--gray)}.c-s1{fill:var(--s1)}.c-s2{fill:var(--s2)}
.ring{stroke:var(--surface);stroke-width:2}
.meter{height:6px;background:var(--grid);overflow:hidden;min-width:90px}.meter>i{display:block;height:100%;background:var(--s1)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
ul.reasons{padding-left:20px;margin:8px 0}ul.reasons li{margin:8px 0}
.small{color:var(--ink2);font-size:13px}.foot{color:var(--ink3);font-size:12px;margin-top:32px;padding-top:16px;border-top:1px solid var(--line);word-break:break-all}
.fan-o{fill:var(--s1);opacity:.1}.fan-i{fill:var(--s1);opacity:.22}.med{fill:none;stroke:var(--s1);stroke-width:2}
.goal-line{stroke:var(--ink);stroke-width:1.5;stroke-dasharray:5 4}.ico-y{fill:var(--s1)}.ico-n{fill:var(--grid)}
.big{font:400 56px/1 var(--serif)}.note{border-left:2px solid var(--ink);padding:4px 0 4px 12px;margin:12px 0}
ol.steps{padding-left:22px;margin:8px 0}ol.steps li{margin:10px 0}
@media print{section{break-inside:avoid}}
"""


def _e(x: object) -> str:
    return html.escape(str(x))


def _money(x: float, currency: str) -> str:
    sign = "-" if x < -0.5 else ""
    return f"{sign}{abs(x):,.0f} {currency}"


def _fit(label: str, width_px: int, char_px: float = 6.6) -> str:
    """Shorten a chart label to fit `width_px` (12px system font); the full text stays in the tooltip."""
    max_chars = int((width_px - 14) / char_px)
    return label if len(label) <= max_chars else label[:max_chars - 1].rstrip() + "…"


_DARK_SEGMENTS = ("c-equity", "c-crypto", "c-bond")      # white label on these (site shades zinc-900/700/500)


def _svg_split_bar(parts: list[tuple[str, str, float]], width: int = 640) -> str:
    """One horizontal stacked bar: parts = (css class, label, share). 2px surface gaps between segments."""
    x, h, out = 0.0, 36, []
    shown = [p for p in parts if p[2] > 0.0005]
    for cls, label, share in shown:
        w = share * width
        out.append(f'<g><title>{_e(label)}: {share:.1%}</title>'
                   f'<rect x="{x + 1:.1f}" y="0" width="{max(w - 2, 1):.1f}" height="{h}" class="{cls}"/></g>')
        if w > 90:
            out.append(f'<text x="{x + w / 2:.1f}" y="{h / 2 + 4}" text-anchor="middle" style="fill:{"#ffffff" if cls in _DARK_SEGMENTS else "#18181b"};font-weight:600">'
                       f'{_e(label)} {share:.0%}</text>')
        x += w
    return f'<div class="sv"><svg class="wide" viewBox="0 0 {width} {h}" role="img" aria-label="Split between asset classes">{"".join(out)}</svg></div>'


def _svg_hbar(rows: list[tuple[str, float, str, str, str]], width: int = 640, label_w: int = 190,
              vmax: float | None = None, aria: str = "", value_w: int = 110) -> str:
    """Horizontal bars. rows = (label, value >= 0, css class, value text, tooltip)."""
    bar_h, gap = 16, 12
    vmax = vmax or max((r[1] for r in rows), default=1.0) or 1.0
    avail = width - label_w - value_w
    out = []
    for i, (label, value, cls, text, tip) in enumerate(rows):
        y = gap + i * (bar_h + gap)
        w = max(value / vmax * avail, 0)
        out.append(f'<g><title>{_e(tip)}</title><text x="{label_w - 10}" y="{y + 12}" text-anchor="end">{_e(_fit(label, label_w))}</text>')
        if w > 0.5:
            out.append(f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" class="{cls}"/>')
        out.append(f'<text class="v" x="{label_w + w + 8:.1f}" y="{y + 12}">{_e(text)}</text></g>')
    height = gap + len(rows) * (bar_h + gap)
    return f'<div class="sv"><svg class="wide" viewBox="0 0 {width} {height}" role="img" aria-label="{_e(aria)}">{"".join(out)}</svg></div>'


def _svg_paired(rows: list[tuple[str, float, float, str]], cls_a: str, cls_b: str, width: int = 640,
                label_w: int = 190, aria: str = "") -> str:
    """Two thin bars per row. rows = (label, a, b, tooltip). a is drawn on top, b below."""
    bar_h, inner, gap = 10, 3, 16
    vmax = max([max(r[1], r[2]) for r in rows] + [0.01])
    avail = width - label_w - 70
    out = []
    for i, (label, a, b, tip) in enumerate(rows):
        y = gap + i * (2 * bar_h + inner + gap)
        out.append(f'<g><title>{_e(tip)}</title><text x="{label_w - 10}" y="{y + bar_h + 3}" text-anchor="end">{_e(_fit(label, label_w))}</text>')
        for k, (val, cls) in enumerate(((a, cls_a), (b, cls_b))):
            yy = y + k * (bar_h + inner)
            w = max(val / vmax * avail, 0)
            if w > 0.5:
                out.append(f'<rect x="{label_w}" y="{yy}" width="{w:.1f}" height="{bar_h}" class="{cls}"/>')
            out.append(f'<text class="v" x="{label_w + w + 6:.1f}" y="{yy + 9}" style="font-size:11px">{val:.0%}</text>')
        out.append("</g>")
    height = gap + len(rows) * (2 * bar_h + inner + gap)
    return f'<div class="sv"><svg class="wide" viewBox="0 0 {width} {height}" role="img" aria-label="{_e(aria)}">{"".join(out)}</svg></div>'


def _nice_ticks(vmax: float, max_ticks: int = 5) -> list[float]:
    """Round tick values from 0 up to just above vmax (steps like 1%, 2%, 5%, 10%)."""
    for step in (0.01, 0.02, 0.05, 0.10, 0.20):
        n = int(vmax / step + 0.999)
        if n + 1 <= max_ticks:
            return [k * step for k in range(n + 1)]
    return [0.0, vmax]


def _svg_menu(points: list[MenuPoint], field: str, width: int = 330, height: int = 230) -> str:
    """Options along the stock axis. Gray line = menu, blue = recommended, orange square = simple fixed mix."""
    l, r, t, b = 44, 14, 16, 34
    opts = sorted([p for p in points if p.kind in ("option", "class_default")], key=lambda p: p.equity)
    xticks = _nice_ticks(max(0.1, max(p.equity for p in points)))
    yticks = _nice_ticks(max(getattr(p, field) for p in points) * 1.1)
    xmax, ymax = xticks[-1], yticks[-1]
    sx = lambda v: l + v / xmax * (width - l - r)
    sy = lambda v: height - b - v / ymax * (height - t - b)
    out = []
    for yv in yticks:
        out.append(f'<line class="grid" x1="{l}" x2="{width - r}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}"/>'
                   f'<text x="{l - 6}" y="{sy(yv) + 4:.1f}" text-anchor="end">{yv:.0%}</text>')
    for xv in xticks:
        out.append(f'<text x="{sx(xv):.1f}" y="{height - 14}" text-anchor="middle">{xv:.0%}</text>')
    out.append(f'<line class="axis" x1="{l}" x2="{width - r}" y1="{height - b}" y2="{height - b}"/>')
    out.append(f'<text x="{(l + width - r) / 2:.1f}" y="{height - 1}" text-anchor="middle">share in stocks</text>')
    if len(opts) > 1:
        d = " ".join(f"{sx(p.equity):.1f},{sy(getattr(p, field)):.1f}" for p in opts)
        out.append(f'<polyline points="{d}" fill="none" style="stroke:var(--gray)" stroke-width="2"/>')
    for p in opts:
        out.append(f'<g><title>Option: {p.equity:.0%} stocks, {field_label(field)} {getattr(p, field):.1%}</title>'
                   f'<circle cx="{sx(p.equity):.1f}" cy="{sy(getattr(p, field)):.1f}" r="4" class="c-gray ring"/></g>')
    for p in points:
        if p.kind == "baseline":
            x, y = sx(p.equity), sy(getattr(p, field))
            out.append(f'<g><title>Simple fixed mix: {p.equity:.0%} stocks, {field_label(field)} {getattr(p, field):.1%}</title>'
                       f'<rect x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" class="c-s2 ring"/>'
                       f'<text x="{x:.1f}" y="{y - 11:.1f}" text-anchor="end">Simple mix</text></g>')
    for p in points:
        if p.kind == "recommended":
            x, y = sx(p.equity), sy(getattr(p, field))
            anchor = "end" if p.equity > xmax * 0.25 else "start"      # label up and to the left of a rising line
            dx = -10 if anchor == "end" else 10
            out.append(f'<g><title>Recommended: {p.equity:.0%} stocks, {field_label(field)} {getattr(p, field):.1%}</title>'
                       f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" class="c-s1 ring"/>'
                       f'<text class="v" x="{x + dx:.1f}" y="{y - 12:.1f}" text-anchor="{anchor}">Recommended</text></g>')
    return f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{field_label(field)} by share in stocks">{"".join(out)}</svg>'


def _svg_icons(filled: int, per_row: int = 20) -> str:
    """100 squares, `filled` of them blue (reached the goal), the rest gray; filled first, row by row."""
    size, gap = 14, 4
    rows = (100 + per_row - 1) // per_row
    out = []
    for i in range(100):
        x, y = (i % per_row) * (size + gap), (i // per_row) * (size + gap)
        out.append(f'<rect x="{x}" y="{y}" width="{size}" height="{size}" class="{"ico-y" if i < filled else "ico-n"}"/>')
    w, h = per_row * (size + gap) - gap, rows * (size + gap) - gap
    return (f'<svg viewBox="0 0 {w} {h}" style="max-width:{w}px" role="img" aria-label="{filled} of 100 simulated futures '
            f'reach the goal">{"".join(out)}</svg>')


def _svg_fan(fan: list[FanPoint], goal: float, currency: str, width: int = 640, height: int = 270) -> str:
    """Wealth over time: 10-90% and 25-75% bands of the simulated futures, the median line, the goal line."""
    if len(fan) < 2:
        return ""
    l, r, t, b = 84, 16, 16, 30
    years = fan[-1].year or 1.0
    top = max([f.p90 for f in fan] + [goal]) * 1.08
    raw = top / 4
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    sx = lambda v: l + v / years * (width - l - r)                       # noqa: E731
    sy = lambda v: t + (1 - v / top) * (height - t - b)                  # noqa: E731
    short = lambda v: (f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e3 else f"{v:.0f}")  # noqa: E731
    out = []
    v = 0.0
    while v <= top:
        out.append(f'<line class="grid" x1="{l}" x2="{width - r}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                   f'<text x="{l - 8}" y="{sy(v) + 4:.1f}" text-anchor="end">{short(v)} {_e(currency)}</text>')
        v += step
    every = max(1, math.ceil(len(fan) / 10))
    for i, f in enumerate(fan):
        if i % every == 0 or i == len(fan) - 1:
            out.append(f'<text x="{sx(f.year):.1f}" y="{height - 10}" text-anchor="middle">'
                       f'{"Today" if f.year == 0 else f"Yr {f.year:g}"}</text>')

    def band(lo: str, hi: str, cls: str) -> str:
        pts = [f"{sx(f.year):.1f},{sy(getattr(f, hi)):.1f}" for f in fan]
        pts += [f"{sx(f.year):.1f},{sy(getattr(f, lo)):.1f}" for f in reversed(fan)]
        return f'<polygon points="{" ".join(pts)}" class="{cls}"/>'

    out.append(band("p10", "p90", "fan-o") + band("p25", "p75", "fan-i"))
    out.append('<polyline class="med" points="' + " ".join(f"{sx(f.year):.1f},{sy(f.p50):.1f}" for f in fan) + '"/>')
    out.append(f'<line class="goal-line" x1="{l}" x2="{width - r}" y1="{sy(goal):.1f}" y2="{sy(goal):.1f}"/>'
               f'<text class="v" x="{l + 6}" y="{sy(goal) - 6:.1f}">Goal {_e(_money(goal, currency))}</text>')   # left: clear of the fan's end
    seg = (width - l - r) / max(len(fan) - 1, 1)
    for f in fan:                                                        # hover: every percentile for that year
        tip = (f"{'Today' if f.year == 0 else f'Year {f.year:g}'}: 1 in 10 below {_money(f.p10, currency)}; "
               f"half above {_money(f.p50, currency)}; 1 in 10 above {_money(f.p90, currency)}")
        out.append(f'<g><title>{_e(tip)}</title><rect x="{sx(f.year) - seg / 2:.1f}" y="{t}" width="{seg:.1f}" '
                   f'height="{height - t - b}" fill="transparent"/></g>')
    return (f'<div class="sv"><svg class="wide" viewBox="0 0 {width} {height}" role="img" aria-label="Simulated wealth '
            f'over {years:g} years: half the futures end above {_e(_money(fan[-1].p50, currency))}, goal '
            f'{_e(_money(goal, currency))}">{"".join(out)}</svg></div>')


def goal_section_html(gr: GoalReport, currency: str) -> str:
    """The "Your goal" section: chance, outcomes, the size of a miss, levers, method, choice, disclosures."""
    p = [f"<section><h2>Your goal: {_e(_money(gr.goal_amount, currency))} in {_e(_years(gr.horizon_years))}</h2>"
         f"<p class='small'>{_e(gr.label)} {_e(gr.plan)}</p>"
         f"<div class='two' style='align-items:center;margin-top:12px'><div><div class='big'>{_e(gr.chance)}</div>"
         f"<p class='lede'>{_e(gr.headline)}</p></div><div>{_svg_icons(gr.in_100)}"
         f"<div class='legend'><span><i class='sw' style='background:var(--s1)'></i>Reach the goal ({gr.in_100})</span>"
         f"<span><i class='sw' style='background:var(--gray)'></i>Fall short ({100 - gr.in_100})</span></div></div></div>"
         f"<p class='small'>{_e(gr.precision)}</p>"]

    p.append("<h2 style='margin-top:20px'>Where you could end up</h2><div class='tiles'>"
             + "".join(f"<div class='tile'><div class='v' style='font-size:22px'>{_e(x.value)}</div>"
                       f"<div class='k'>{_e(x.label)}</div></div>" for x in gr.outcomes) + "</div>")
    p.append("<div class='legend' style='margin-top:16px'><span><i class='sw' style='background:var(--s1);opacity:.3'></i>"
             "8 in 10 futures inside</span><span><i class='sw' style='background:var(--s1);opacity:.55'></i>Half inside</span>"
             "<span><i class='sw' style='background:var(--s1);height:3px'></i>Middle outcome</span>"
             "<span><i class='sw' style='background:var(--ink);height:2px'></i>Your goal</span></div>"
             + _svg_fan(gr.fan, gr.goal_amount, currency)
             + "<ul class='reasons'>" + "".join(f"<li>{_e(x.text)}</li>" for x in gr.outcomes) + "</ul>")

    p.append("<h2 style='margin-top:20px'>How bad could a miss be?</h2><ul class='reasons'>"
             + "".join(f"<li><b>{_e(x.label)}: {_e(x.value)}.</b> {_e(x.text)}</li>" for x in gr.severity) + "</ul>")

    if gr.levers:
        p.append(f"<h2 style='margin-top:20px'>What would change your odds</h2><p class='small'>{_e(gr.levers_intro)}</p>"
                 f"<table><tr><th>Change</th><th class='n'>Chance</th></tr>"
                 f"<tr><td>Your plan as it is</td><td class='n'><b>{_e(gr.chance)}</b></td></tr>"
                 + "".join(f"<tr><td>{_e(lv.change)}</td><td class='n'>{_e(lv.chance)}</td></tr>" for lv in gr.levers)
                 + "</table>")

    p.append("<h2 style='margin-top:20px'>How we calculated this</h2><ol class='steps'>"
             + "".join(f"<li><b>{_e(s.title)}.</b> {_e(s.text)}</li>" for s in gr.method) + "</ol>")

    p.append("<h2 style='margin-top:20px'>How this portfolio was chosen</h2><ul class='reasons'>"
             + "".join(f"<li>{_e(c)}</li>" for c in gr.choice) + "</ul>"
             "<details><summary class='small'>Show every shortlisted portfolio</summary><table><tr><th>Portfolio</th>"
             "<th class='n'>Stocks</th><th class='n'>Chance</th><th class='n'>Half end above</th>"
             "<th class='n'>Worst-5% shortfall</th></tr>")
    for row in gr.shortlist:
        bold = " style='font-weight:650'" if row.chosen else ""
        p.append(f"<tr{bold}><td>{'▸ ' if row.chosen else ''}{_e(row.label[:1].upper() + row.label[1:])}</td>"
                 f"<td class='n'>{row.equity_weight:.0%}</td><td class='n'>{_e(_chance(row.prob_goal))}</td>"
                 f"<td class='n'>{_e(_money(_round_money(row.p50), currency))}</td>"
                 f"<td class='n'>{_e(_money(_round_money(row.worst5_shortfall), currency))}</td></tr>")
    p.append("</table></details>")

    p.append("<div class='note'><p class='small' style='margin:0'><b>About these numbers</b></p><ul class='reasons small'>"
             + "".join(f"<li>{_e(d)}</li>" for d in gr.disclosures) + "</ul></div></section>")
    return "".join(p)


def field_label(field: str) -> str:
    return {"vol": "volatility", "worst_loss": "worst-case loss"}.get(field, field)


def render_html(report: ClientReport, title: str = "Your proposed portfolio") -> str:
    """One self-contained HTML page for the investor. All text is escaped."""
    r, cur = report, report.currency
    ref = f"{r.reference_amount:,.0f} {cur}"
    p: list[str] = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                    f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
                    "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&amp;family=Instrument+Serif&amp;display=swap'>"
                    f"<style>{_CSS}</style></head><body><main>"]
    p.append(f"<h1>{_e(title)}</h1><p class='sub'>Prepared for {_e(r.investor_id)} · risk class {_e(r.risk_class)} · "
             f"market data as of {(r.prices_as_of or r.as_of):%d %b %Y}</p>")
    if r.profile_assessed_on and r.profile_valid_until:
        state = " (expired)" if r.profile_expired else ""
        p.append(f"<p class='sub'>Risk profile assessed on {r.profile_assessed_on:%d %b %Y}, "
                 f"valid until {r.profile_valid_until:%d %b %Y}{state}.</p>")
    news_only = set(r.market_briefing.banners) if r.market_briefing is not None else set()
    for text in r.banners:
        if text not in news_only:                  # the news is not in the download, so neither is its banner
            p.append(f"<div class='banner'>{_e(text)}</div>")

    if r.status == "infeasible":
        p.append(f"<section><h2>No portfolio proposed</h2><p class='lede'>{_e(r.headline)}</p>"
                 + "".join(f"<p>{_e(x.text)}</p>" for x in r.reasons) + "</section>")
        p.append(_footer(r))
        return "".join(p) + "</main></body></html>"

    # ---- 1. summary -------------------------------------------------------------------------
    status_tile = ("<span class='ok'>✔ All limits met</span>" if r.limits_ok
                   else "<span class='bad'>⚠ A limit is not met</span>")
    p.append(f"<section><h2>Summary</h2><p class='lede'>{_e(r.headline)}</p><div class='tiles'>"
             f"<div class='tile'><div class='v'>{r.equity_weight:.0%}</div><div class='k'>in stocks</div></div>"
             f"<div class='tile'><div class='v'>{r.ex_ante_vol:.1%}</div><div class='k'>estimated volatility per year</div></div>"
             f"<div class='tile'><div class='v'>-{r.worst_loss:.0%}</div><div class='k'>worst of {len(r.stress)} hypothetical scenarios "
             f"({_e(_money(-r.worst_loss * r.reference_amount, cur))} per {_e(ref)})</div></div>"
             f"<div class='tile'><div class='v' style='font-size:20px'>{status_tile}</div>"
             f"<div class='k'>checked against your {len(r.limits)} limits</div></div>"
             + (f"<div class='tile'><div class='v'>{_e(r.goal.chance)}</div><div class='k'>simulated chance of reaching "
                f"{_e(_money(r.goal.goal_amount, cur))} (hypothetical)</div></div>" if r.goal else "")
             + "</div></section>")
    if r.goal is not None:
        p.append(goal_section_html(r.goal, cur))

    # ---- 2. what you would hold ---------------------------------------------------------------
    shares = [("equity", "s1", r.equity_weight), ("bond", "s2", r.bond_weight), ("commodity", "s4", r.commodity_weight),
              ("crypto", "s5", r.crypto_weight), ("cash", "s3", r.cash_weight)]
    shown = [x for x in shares if x[2] > 0.0005 or x[0] in ("equity", "bond", "cash")]
    p.append("<section><h2>What you would hold</h2>"
             + _svg_split_bar([(f"c-{c}", CLASS_LABEL[c], w) for c, _, w in shown])
             + "<div class='legend'>"
             + "".join(f"<span><i class='sw' style='background:var(--{var})'></i>{_e(CLASS_LABEL[c])}</span>"
                       for c, var, _ in shown)
             + "</div>")
    bars = [(a.name, a.weight, f"c-{a.asset_class}", f"{a.weight:.1%}",
             f"{a.name}: {a.weight:.1%} ({_money(a.amount, cur)} per {ref})") for a in r.allocation]
    p.append(_svg_hbar(bars, aria="Weight of each holding"))
    has_fees = any(a.expense_ratio > 0 for a in r.allocation)
    p.append("<table><tr><th>Holding</th><th>Type</th><th class='n'>Weight</th><th class='n'>Per " + _e(ref) +
             "</th><th class='n'>Annual fee</th></tr>")
    for a in r.allocation:
        p.append(f"<tr><td>{_e(a.name)}</td><td>{CLASS_LABEL.get(a.asset_class, a.asset_class)}</td><td class='n'>{a.weight:.1%}</td>"
                 f"<td class='n'>{_e(_money(a.amount, cur))}</td><td class='n'>{f'{a.expense_ratio:.2%}' if has_fees else '–'}</td></tr>")
    p.append(f"<tr><th>Total</th><th></th><th class='n'>100%</th><th class='n'>{_e(ref)}</th><th class='n'>{f'{r.ongoing_fee:.2%}' if has_fees else '–'}</th></tr></table></section>")

    # ---- 2b. what you asked about --------------------------------------------------------------
    if r.interest_notes:
        p.append("<section><h2>What you asked about</h2><ul class='reasons'>")
        p.extend(f"<li>{_e(x)}</li>" for x in r.interest_notes)
        p.append("</ul></section>")

    # ---- 3. why this portfolio ---------------------------------------------------------------
    p.append("<section><h2>Why this portfolio</h2><ul class='reasons'>")
    p.extend(f"<li>{_e(x.text)}</li>" for x in r.reasons)
    p.append("</ul></section>")

    # ---- 4. risk: limits + where the risk comes from -------------------------------------------
    p.append("<section><h2>How risky is it?</h2><p class='small'>Your limits and where this portfolio stands against each one.</p>"
             "<table><tr><th>Limit</th><th class='n'>This portfolio</th><th class='n'>Limit</th><th>Use of limit</th><th>Result</th></tr>")
    for c in r.limits:
        usage = ""
        if c.direction == "max" and c.limit > 0:
            usage = f"<div class='meter' title='{c.value / c.limit:.0%} of the limit'><i style='width:{min(c.value / c.limit, 1) * 100:.0f}%'></i></div>"
        sign = "≤" if c.direction == "max" else "≥"
        res = "<span class='ok'>✔ within</span>" if c.ok else "<span class='bad'>⚠ breached</span>"
        p.append(f"<tr><td>{_e(c.name)}<div class='small'>{_e(c.source)}</div></td><td class='n'>{c.value:.1%}</td>"
                 f"<td class='n'>{sign} {c.limit:.1%}</td><td>{usage}</td><td>{res}</td></tr>")
    p.append("</table>")
    rows = [(a.name, a.weight, a.risk_share, f"{a.name}: {a.weight:.0%} of the money, {a.risk_share:.0%} of the risk")
            for a in r.allocation]
    p.append("<h2 style='margin-top:20px'>Where the risk comes from</h2>"
             "<p class='small'>A holding can be a small part of your money and a large part of your risk. "
             "This compares each holding's share of the money with its share of the portfolio's risk.</p>"
             "<div class='legend'><span><i class='sw' style='background:var(--gray)'></i>Share of money</span>"
             "<span><i class='sw' style='background:var(--s1)'></i>Share of risk</span></div>"
             + _svg_paired(rows, "c-gray", "c-s1", aria="Share of money versus share of risk per holding"))
    f = r.risk_facts
    p.append(f"<p class='small'>Historical estimates: a bad single day (1-in-20) lost about {f['hist_var_1d']:.1%} or more; the average "
             f"of the worst days lost {f['hist_cvar_1d']:.1%}; the largest fall from peak to bottom in the data window was "
             f"{f['hist_max_drawdown']:.1%}. If all markets moved together more than usual, volatility could reach "
             f"{f['stressed_correlation_vol']:.1%}.</p></section>")

    # ---- 5. what could happen ---------------------------------------------------------------
    vmax = max([abs(s.portfolio_return) for s in r.stress] + [0.01])
    srows = [(s.name, abs(min(s.portfolio_return, 0)), "c-s1",
              f"{s.portfolio_return:+.1%} ({_money(s.amount_change, cur)})", f"{s.description}: {s.portfolio_return:+.1%}")
             for s in r.stress]
    p.append("<section><h2>What could happen</h2><p class='small'>Hypothetical shocks, not forecasts. "
             f"Money amounts are for {_e(ref)} invested.</p>" + _svg_hbar(srows, label_w=190, vmax=vmax, aria="Loss in each scenario", value_w=190)
             + "<table><tr><th>Scenario</th><th>What we assume</th><th class='n'>Result</th></tr>")
    for s in r.stress:
        p.append(f"<tr><td>{_e(s.name)}</td><td>{_e(s.description)}</td><td class='n'>{s.portfolio_return:+.1%}</td></tr>")
    p.append("</table>")
    if r.limit_scenario:
        p.append(f"<p class='small'>Your loss limit applies to one test: <b>{_e(r.limit_scenario)}</b>. "
                 "The other scenarios are shown for information and are not limited, so the worst of them can be larger.</p>")
    if r.reverse_stress_equity_drop is not None:
        p.append(f"<p class='small'>Put the other way round: stock markets would have to fall about {r.reverse_stress_equity_drop:.0%} "
                 "(with everything else unchanged) before this portfolio reached the loss limit you set.</p>")
    p.append("</section>")

    # ---- 6. options considered ---------------------------------------------------------------
    p.append("<section><h2>Options we considered</h2><p class='small'>We built a range of portfolios from cautious to bolder, each with "
             "the lowest volatility we could find for its share of stocks. More stocks means more risk. "
             "The 'simple mix' is a naive split with no optimisation.</p>"
             "<div class='legend'><span><i class='sw' style='background:var(--gray)'></i>Options considered</span>"
             "<span><i class='sw' style='background:var(--s1)'></i>Recommended</span>"
             "<span><i class='sw' style='background:var(--s2)'></i>Simple mix</span></div><div class='two'>"
             f"<div><h2 style='font-size:15px'>Estimated volatility</h2>{_svg_menu(r.menu, 'vol')}</div>"
             f"<div><h2 style='font-size:15px'>Worst hypothetical loss</h2>{_svg_menu(r.menu, 'worst_loss')}</div></div>"
             "<details><summary class='small'>Show the numbers</summary><table><tr><th>Option</th><th class='n'>Stocks</th><th class='n'>Volatility</th><th class='n'>Worst loss</th></tr>")
    labels = {"option": "Option", "class_default": "Option at your class target", "baseline": "Simple mix", "recommended": "Recommended"}
    for m in sorted(r.menu, key=lambda m: (m.equity, m.kind != "recommended")):
        bold = " style='font-weight:650'" if m.kind == "recommended" else ""
        p.append(f"<tr{bold}><td>{labels[m.kind]}</td><td class='n'>{m.equity:.0%}</td><td class='n'>{m.vol:.1%}</td>"
                 f"<td class='n'>-{m.worst_loss:.0%}</td></tr>")
    p.append("</table></details></section>")

    # ---- 7. changes and costs ------------------------------------------------------------------
    p.append("<section><h2>Changes and costs</h2>")
    if r.trades:
        trows = [(t.name, t.current, t.proposed, f"{t.name}: {t.current:.0%} now, {t.proposed:.0%} proposed") for t in r.trades]
        p.append("<div class='legend'><span><i class='sw' style='background:var(--gray)'></i>Today</span>"
                 "<span><i class='sw' style='background:var(--s1)'></i>Proposed</span></div>"
                 + _svg_paired(trows, "c-gray", "c-s1", aria="Current versus proposed weights")
                 + "<table><tr><th>Holding</th><th class='n'>Today</th><th class='n'>Proposed</th><th class='n'>Change</th></tr>")
        for t in r.trades:
            p.append(f"<tr><td>{_e(t.name)}</td><td class='n'>{t.current:.1%}</td><td class='n'>{t.proposed:.1%}</td>"
                     f"<td class='n'>{t.change * 100:+.1f} pts</td></tr>")
        p.append("</table>")
    else:
        p.append("<p class='small'>No current holdings were provided, so this is shown as a first purchase.</p>")
    cost = ""
    if r.turnover is not None and r.one_off_trading_cost is not None:
        cost = (f"Moving to this portfolio means trading about {r.turnover:.0%} of it, at an estimated one-off cost of "
                f"{r.one_off_trading_cost:.3%} ({_e(_money(r.one_off_trading_cost * r.reference_amount, cur))} per {_e(ref)}). ")
    fees = (f"Fund fees add up to about {r.ongoing_fee:.2%} per year "
            f"({_e(_money(r.ongoing_fee * r.reference_amount, cur))} per {_e(ref)})." if has_fees
            else "Fund fee data was not available for this universe, so ongoing fees are not shown.")
    p.append(f"<p>{cost}{fees}</p></section>")

    # The market briefing and "What changed" are not part of the download (Bideci, 20 Oct): they live on the site's
    # News tab, which shows the latest news; a report keeps only what the engine decided.

    # ---- 8. disclosures ----------------------------------------------------------------------
    p.append("<section><h2>Assumptions and important information</h2><ul class='reasons'>"
             + "".join(f"<li>{_e(d)}</li>" for d in r.disclosures)
             + "<li><b>Volatility</b> is the typical size of the portfolio's yearly ups and downs. "
               "<b>Stress scenario</b> is a made-up market shock used to test the portfolio. "
               "<b>Risk share</b> is the part of total portfolio variance a holding is responsible for.</li></ul></section>")
    p.append(_footer(r))
    return "".join(p).replace("<table>", "<div class='tw'><table>").replace("</table>", "</table></div>") + "</main></body></html>"


def _footer(r: ClientReport) -> str:
    return (f"<p class='foot'>Generated by EgoQuant, engine {_e(r.engine_version)}, data source: {_e(r.data_source)}. "
            f"Reproducibility fingerprint (same inputs give the same fingerprint): {_e(r.input_hash)}</p>")


def save_report(report: ClientReport, html_path: str, json_path: str | None = None) -> None:
    """Write the HTML page and optionally the JSON version of the same report."""
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(report))
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.write(report.model_dump_json(indent=2))
