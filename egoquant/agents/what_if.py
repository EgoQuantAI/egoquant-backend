"""What-If Agent: re-runs the engine for ONE hypothetical change. Deterministic, no LLM.

The advisor chat's LLM may only (a) translate a hypothetical question into a WhatIfScenario and (b) explain the
WhatIfResult. Every number the investor sees comes from here.

    POST /portfolio returns `what_if_context`: the inputs and results of that build, HMAC-sealed (kind
    "what_if_inputs", optionally bound to the signed-in user). It is never shown to the LLM.

    validate_scenario(args, base)  ->  WhatIfScenario   (or WhatIfInvalid with a plain-language reason)
    run_what_if(token, args, ...)  ->  WhatIfResult     status ok | blocked | invalid | unavailable

Scenario fields (all optional, unknown fields rejected): extra_monthly_contribution, one_time_contribution,
horizon_years, goal_amount, exclude_assets, mode. There is NO field for the risk class, a cap or a limit, so a
scenario can never loosen them ("tighten-only"): the questionnaire answers are never changed (so the risk class
and every suitability limit stay the same), Agent B's limits come from the same inputs, and the Risk Engine
re-checks the scenario's portfolio against the base portfolio's hard limits. A failed re-check returns status
"blocked" with the reasons and NO probabilities.

Modes
  same_portfolio (default)  the base portfolio's weights (minus excluded assets, renormalised), re-simulated.
  reoptimize                Agent A -> Agent B shortlist -> Monte Carlo choice re-run with the changed inputs
                            (the same route code as /portfolio, without the news briefing; WHAT_IF_SELECTION_PATHS
                            paths for the choice), then evaluated exactly like same_portfolio.

Common random numbers. Baseline and scenario are evaluated on the SAME simulated futures: the same completed
price history, the same block draws (the goal simulation's own seed and path count), a longer horizon extending
every path with further blocks from seed + 1 (as goal.goal_levers does). The difference is the scenario, not luck.
In same_portfolio mode the baseline is bit-identical to the report's own goal simulation (same history, seed and
WHAT_IF_PATHS = the report's 10,000 paths; a single portfolio takes well under a second).

Sampler. A report simulated with the regime-aware sampler (egoquant/regime, EGOQUANT_REGIME_MC) carries its
sampler record (model version, SHA-256, today's regime probabilities) in the sealed context. A what-if replays it with
exactly that sampler, whatever the flag says now, so the baseline still equals the report's chance; reoptimize makes
Agent A's re-selection use it too (regime_hook.forced_sampler). A report without a record used the block bootstrap,
and so does its what-if.

Reused, not reimplemented: goal.complete_history, equilibrium_means (and the rate anchor when present),
block_returns, stream_wealth (simulate_wealth + max_drawdowns without the wealth matrices); Agent C's get_price_history; the /portfolio route's
_build_portfolio; RiskEngine.review. The history preparation follows Agent A's run_goal_based step by step (as the
goal walkthrough's _rederive does), without changing Agent A.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer

from egoquant.agents.quant import goal as gq
from egoquant.models import ASSET_CLASSES, AgentConfig, AssetInfo, Constraint, PortfolioProposal
from egoquant.regime import hook as regime_hook

logger = logging.getLogger(__name__)
audit = logging.getLogger("egoquant.audit.what_if")

ENGINE_VERSION = "what-if-0.1.0"
CONTEXT_KIND = "what_if_inputs"
WHAT_IF_PATHS = 10_000              # evaluation paths: the report's own count, so the baseline equals the report
WHAT_IF_SELECTION_PATHS = 3_000     # reoptimize: paths for Agent A's choice among the shortlist (speed)
WHAT_IF_TIMEOUT_S = 20.0            # hard timeout per run
RATE_LIMIT_RUNS = 10                # per user (or per base portfolio when anonymous) ...
RATE_LIMIT_WINDOW_S = 600.0         # ... per 10 minutes
MAX_EXCLUDE = 10
MAX_EXTRA_MONTHLY = 50_000.0
MAX_ONE_TIME = 10_000_000.0
MAX_GOAL = 100_000_000.0
HORIZON_BOUNDS = (1.0, 40.0)
# Names an LLM might try to slip in. Rejected like any unknown field, with this explanation.
FORBIDDEN_FIELDS = {"risk_class", "c_level", "class", "max_equity", "max_crypto", "max_commodity", "max_stress_loss",
                    "max_r_level", "r_level", "limits", "caps", "cap", "risk_level", "category", "suitability"}

Status = Literal["ok", "blocked", "invalid", "unavailable"]


# ==========================================================================================================
# Models
# ==========================================================================================================
class WhatIfScenario(BaseModel):
    """One hypothetical change. All fields optional; unknown fields are rejected (no risk class or caps)."""

    model_config = ConfigDict(extra="forbid")

    extra_monthly_contribution: float | None = Field(None, allow_inf_nan=False)
    one_time_contribution: float | None = Field(None, allow_inf_nan=False)
    horizon_years: float | None = Field(None, allow_inf_nan=False)
    goal_amount: float | None = Field(None, allow_inf_nan=False)
    exclude_assets: list[str] | None = Field(None, max_length=MAX_EXCLUDE)
    mode: Literal["same_portfolio", "reoptimize"] = "same_portfolio"


class WhatIfInvalid(ValueError):
    """The scenario cannot be run. `reason` is plain English, safe to show (never quotes the raw input)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Outcome(BaseModel):
    goal_frequency: int | None          # "X in 100" (the report's own rounding); None without a goal
    goal_chance: str | None             # as the report shows it: "68%", "less than 1%", ...
    median_final: float                 # final value, middle outcome
    p5_final: float                     # final value reached or beaten in 95 of 100 futures
    drawdown_p95: float                 # worst fall from a previous high in the worst 5 of 100 futures


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str]                  # the Risk Engine's violations (empty when approved)


class WhatIfResult(BaseModel):
    """What the chat and POST /what-if return. HYPOTHETICAL: a simulation on past data, not a forecast."""

    status: Status
    reason: str | None = None           # why, when not ok
    scenario: dict | None = None        # the normalised scenario that ran
    scenario_text: str | None = None    # the scenario in plain words
    baseline: Outcome | None = None
    scenario_outcome: Outcome | None = None
    baseline_goal_frequency: int | None = None
    scenario_goal_frequency: int | None = None
    goal_amount: float | None = None
    horizon_years: float | None = None
    portfolio_changed: bool = False
    new_weights: dict[str, float] | None = None     # reoptimize or excluded assets: the scenario's weights
    risk_engine: RiskVerdict | None = None
    n_paths: int | None = None
    selection_paths: int | None = None  # reoptimize: paths Agent A used to choose
    seed: int | None = None
    base_report_id: str | None = None   # the saved report the base portfolio came from (signed-in users)
    engine_version: str = ENGINE_VERSION
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    label: str = "hypothetical simulation on past market data; not a forecast or a guarantee"
    sampler: dict | None = None         # regime sampler record, when the base report used it (else left out)

    @model_serializer(mode="wrap")
    def _omit_unset_sampler(self, handler):
        data = handler(self)
        if self.sampler is None and isinstance(data, dict):
            data.pop("sampler", None)
        return data


# ==========================================================================================================
# The sealed base context (built by /portfolio, never shown to the LLM)
# ==========================================================================================================
def build_what_if_facts(*, body: dict, run, as_of: date, user_id: str | None, ibkr_source: str | None,
                        assets: list[AssetInfo], config: AgentConfig | None = None) -> dict:
    """What a later what-if needs from one /portfolio build. `body` is the PortfolioIn as JSON WITHOUT any raw
    IBKR report or Flex token (the caller drops them)."""
    cfg = config or AgentConfig()
    final = run.result.final
    weights = {a: round(float(w), 10) for a, w in final.weights.items() if w > 1e-9}
    instruments = run.agent_c.instruments
    g = run.goal_analysis
    sampler = g.sampler if g is not None else None          # set only when the regime sampler ran
    if g is not None and run.screening is not None:
        menu = run.result.menu
        held = sorted({a for i in run.screening.approved for a, w in menu.portfolios[i].weights.items() if w > 1e-9})
        goal = g.spec.model_dump()
    else:
        held, goal = sorted(weights), None
    universe = list(run.selection.tickers)
    market = next((m for m in gq.MARKET_PROXIES if m in universe), None) if cfg.goal_hindsight_correction else None
    goal_assets = sorted(set(held) | set(weights) | ({market} if market else set()))
    info = {a.asset_id: a for a in assets}
    facts = {
        "kind": CONTEXT_KIND, "version": 1, "user_id": user_id, "as_of": as_of.isoformat(),
        "data_source": final.data_source, "c_level": run.profile.c_level, "max_r_level": run.profile.max_r_level,
        "inputs": body, "ibkr": ibkr_source, "approved": run.approved, "weights": weights, "goal": goal,
        "goal_assets": goal_assets, "market": market, "interest_areas": list(run.selection.interest_areas),
        "classes": {t: instruments[t].asset_class for t in set(goal_assets) | set(universe) if t in instruments},
        "r_levels": {t: instruments[t].r_level for t in weights if t in instruments},
        "fees": {t: float(info[t].expense_ratio) for t in goal_assets if t in info},
        "assets": [info[t].model_dump() for t in sorted(weights) if t in info],
        "hard_constraints": [c.model_dump() for c in run.review_constraints if c.hard],
        "base_prob_goal": next((o.prob_goal for o in g.outcomes if o.menu_index == g.chosen_index), None) if g else None,
    }
    if sampler is not None:
        facts["sampler"] = sampler
    if g is not None and g.rate_anchor is not None:          # the report's bond / cash rate anchor, replayed exactly
        facts["rate_anchor"] = {"short_rate": g.rate_anchor.short_rate, "term_premium": g.rate_anchor.term_premium}
    return facts


def seal_what_if_context(facts: dict) -> str:
    from egoquant.advisor.context import seal_context
    return seal_context(facts)


def open_what_if_context(token: str | None) -> dict:
    """The base facts, or WhatIfInvalid (missing, bad seal, expired, or not a what-if context)."""
    from egoquant.advisor.context import ContextError, open_context
    if not token:
        raise WhatIfInvalid("There is no portfolio to simulate yet. Build one on the Portfolio page first.")
    try:
        facts = open_context(token)
    except ContextError:
        raise WhatIfInvalid("The saved portfolio could not be verified (it may be old). Rebuild it on the "
                            "Portfolio page, then ask again.") from None
    if not isinstance(facts, dict) or facts.get("kind") != CONTEXT_KIND:
        raise WhatIfInvalid("The saved portfolio could not be verified. Rebuild it on the Portfolio page.")
    return facts


# ==========================================================================================================
# Validation (untrusted arguments: they come from an LLM that read the user's text)
# ==========================================================================================================
def validate_scenario(args: Any, base: dict) -> WhatIfScenario:
    """Whitelist, bounds and the tighten-only rule. Raises WhatIfInvalid with a plain reason; never crashes."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise WhatIfInvalid("The scenario could not be read.") from None
    if not isinstance(args, dict):
        raise WhatIfInvalid("The scenario could not be read.")
    keys = {str(k).lower() for k in args}
    if keys & FORBIDDEN_FIELDS:
        raise WhatIfInvalid("A what-if can change money, time, the goal or which holdings to leave out, but never "
                            "the risk category or any limit. Those come only from the questionnaire.")
    try:
        sc = WhatIfScenario.model_validate(args)
    except ValidationError as err:
        fields = sorted({str(e["loc"][0]) for e in err.errors() if e.get("loc")})
        unknown = [f for f in fields if f not in WhatIfScenario.model_fields]
        if unknown:
            raise WhatIfInvalid("The engine can only simulate changes to the monthly amount, a one-time amount, the "
                                "number of years, the goal amount, leaving holdings out, or re-optimising.") from None
        raise WhatIfInvalid("Some scenario values are not valid numbers or choices (" + ", ".join(fields) + ").") from None

    goal = base.get("goal") or {}
    inputs = base.get("inputs") or {}
    start = float(goal.get("start_amount") or (inputs.get("answers") or {}).get("amount_invested") or 0.0)
    monthly = float(goal.get("monthly_flow") if goal else inputs.get("monthly_flow") or 0.0)
    if sc.extra_monthly_contribution is not None:
        low = -max(monthly, 0.0)
        if not (low - 1e-9 <= sc.extra_monthly_contribution <= MAX_EXTRA_MONTHLY):
            raise WhatIfInvalid(f"The monthly change must be between {low:,.0f} and +{MAX_EXTRA_MONTHLY:,.0f}: it can "
                                "remove the current monthly contribution, not go below it.")
    if sc.one_time_contribution is not None:
        if sc.one_time_contribution > MAX_ONE_TIME:
            raise WhatIfInvalid(f"A one-time amount can be at most {MAX_ONE_TIME:,.0f}.")
        if start + sc.one_time_contribution <= 0:
            raise WhatIfInvalid("That withdrawal would leave nothing invested, so there is nothing to simulate.")
    if sc.horizon_years is not None and not (HORIZON_BOUNDS[0] <= sc.horizon_years <= HORIZON_BOUNDS[1]):
        raise WhatIfInvalid(f"The number of years must be between {HORIZON_BOUNDS[0]:g} and {HORIZON_BOUNDS[1]:g}.")
    if sc.goal_amount is not None and not (0 < sc.goal_amount <= MAX_GOAL):
        raise WhatIfInvalid(f"The goal must be above 0 and at most {MAX_GOAL:,.0f}.")
    if sc.exclude_assets:
        held = set(base.get("weights") or {})
        out = []
        for raw in sc.exclude_assets:
            item = str(raw).strip()
            if item.lower() in ASSET_CLASSES:
                out.append(item.lower())
            elif item.upper() in held:
                out.append(item.upper())
            else:
                raise WhatIfInvalid("Only holdings that are in your current portfolio, or whole asset classes (stocks, "
                                    "bonds, cash, commodities, crypto), can be left out.")
        sc = sc.model_copy(update={"exclude_assets": sorted(set(out))})
    changes = [sc.extra_monthly_contribution, sc.one_time_contribution, sc.horizon_years, sc.goal_amount,
               sc.exclude_assets]
    if all(v in (None, [], 0, 0.0) for v in changes) and sc.mode == "same_portfolio":
        raise WhatIfInvalid("The scenario does not change anything, so it would give the same result as now.")
    if not goal and sc.goal_amount is None:
        raise WhatIfInvalid("This portfolio was built without a goal. Give a goal amount to simulate the chance of "
                            "reaching it.")
    return sc


def describe(sc: WhatIfScenario, base: dict) -> str:
    """The scenario in plain words (no numbers beyond the scenario's own)."""
    parts = []
    if sc.extra_monthly_contribution:
        parts.append(f"{'add' if sc.extra_monthly_contribution > 0 else 'reduce the monthly amount by'} "
                     f"{abs(sc.extra_monthly_contribution):,.0f} a month" + (" more" if sc.extra_monthly_contribution > 0 else ""))
    if sc.one_time_contribution:
        parts.append(f"{'add' if sc.one_time_contribution > 0 else 'take out'} {abs(sc.one_time_contribution):,.0f} now")
    if sc.horizon_years:
        parts.append(f"a horizon of {sc.horizon_years:g} years")
    if sc.goal_amount:
        parts.append(f"a goal of {sc.goal_amount:,.0f}")
    if sc.exclude_assets:
        parts.append("leave out " + ", ".join(sc.exclude_assets))
    parts.append("re-optimise the portfolio" if sc.mode == "reoptimize" else "keep the same portfolio")
    return "; ".join(parts)


# ==========================================================================================================
# Simulation (common random numbers) and Risk Engine re-check
# ==========================================================================================================
def _specs(base: dict, sc: WhatIfScenario) -> tuple[gq.GoalSpec, gq.GoalSpec]:
    goal = base.get("goal")
    inputs = base.get("inputs") or {}
    if goal:
        base_spec = gq.GoalSpec(**goal)
    else:   # built without a goal: the scenario's goal is measured on the base plan
        base_spec = gq.GoalSpec(start_amount=float((inputs.get("answers") or {})["amount_invested"]),
                                monthly_flow=float(inputs.get("monthly_flow") or 0.0),
                                goal_amount=float(sc.goal_amount), horizon_years=float(inputs["horizon_years"]))
    scn = base_spec.model_copy(update={
        "monthly_flow": base_spec.monthly_flow + (sc.extra_monthly_contribution or 0.0),
        "start_amount": base_spec.start_amount + (sc.one_time_contribution or 0.0),
        "horizon_years": sc.horizon_years or base_spec.horizon_years,
        "goal_amount": sc.goal_amount or base_spec.goal_amount,
    })
    return base_spec, scn


def _excluded_tickers(sc: WhatIfScenario, classes: dict[str, str], tickers) -> set[str]:
    out = set()
    for item in sc.exclude_assets or []:
        if item in ASSET_CLASSES:
            out |= {t for t in tickers if classes.get(t) == item}
        else:
            out.add(item)
    return out


def _renormalise(weights: dict[str, float], drop: set[str]) -> dict[str, float]:
    kept = {t: w for t, w in weights.items() if t not in drop and w > 1e-12}
    total = sum(kept.values())
    if total <= 0:
        raise WhatIfInvalid("Leaving those out would leave nothing in the portfolio.")
    return {t: w / total for t, w in kept.items()}


def _market_agent(base: dict, tickers: list[str]):
    """Agent C set up like the /portfolio build (same profile limits, interests and data mode), holding `tickers`."""
    from egoquant.agents.market_intel import InvestorInterests, MarketIntelligenceAgent, SyntheticPriceSource
    from egoquant.risk_profile import to_agent_c_limits
    import egoquant_routes as routes

    answers_in = routes.AnswersIn(**base["inputs"]["answers"])
    answers = answers_in.to_answers()
    profile = routes._score(answers, answers_in.game_input())       # the Risk Game the build used, if any
    synthetic = base.get("data_source") == "synthetic" or routes._data_mode() == "synthetic"
    interests = InvestorInterests(investor_id=routes.INVESTOR_ID, interest_areas=base["interest_areas"],
                                  trading_platforms=list((base["inputs"].get("trading_platforms") or [])),
                                  held_assets=sorted(tickers), **to_agent_c_limits(profile, routes.PROFILE_CONFIG))
    directory = "auto" if synthetic else routes._directory_for_request()
    return MarketIntelligenceAgent(interests, source=SyntheticPriceSource() if synthetic else None, directory=directory)


def goal_history(base: dict, columns: list[str], cfg: AgentConfig):
    """The goal simulation's history for `columns`, prepared exactly like Agent A's run_goal_based."""
    as_of = date.fromisoformat(base["as_of"])
    agent_c = _market_agent(base, columns)
    prices = agent_c.get_price_history(columns, as_of, cfg.goal_history_days)
    missing = sorted(set(columns) - set(prices.columns))
    if missing:
        raise WhatIfInvalid("Price history for part of the portfolio is not available right now. Try again later.")
    daily, _, _ = gq.complete_history(prices[columns], cfg.goal_block_days, cfg.goal_seed)
    classes = {t: agent_c.instruments[t].asset_class for t in columns if t in agent_c.instruments}
    market = base.get("market")
    if market in daily.columns:
        daily, _, _ = gq.equilibrium_means(daily, classes, market)
    # The bond / cash rate anchor exactly as the report used it (its rate is in the sealed context); a report
    # built without it (before it existed, or with no short-rate fund) is replayed without it.
    anchor = base.get("rate_anchor")
    if anchor:
        daily, _ = gq.anchor_rate_means(daily, classes, anchor["short_rate"], anchor["term_premium"])
    if len(daily) < cfg.goal_min_history_days:
        raise WhatIfInvalid("There is not enough price history to simulate this portfolio.")
    return daily, agent_c


def _draws(n_blocks: int, n_paths: int, months_base: int, months_scn: int, seed: int, regime=None):
    """(base draws, extra months or None). The base draws are exactly analyze_goal's; a longer scenario continues
    every path with `extra` from seed + 1 (no stacked copy: goal.stream_wealth reads both). A shorter scenario uses
    the first months of the base draws. regime: the report's regime decision (same chains as the report;
    extensions continue each path's chain)."""
    base = regime.draws(n_blocks, n_paths, months_base, seed) if regime is not None else None
    if base is None:
        rng = np.random.default_rng(seed)
        base = rng.integers(0, n_blocks, size=(n_paths, months_base))
    if months_scn <= months_base:
        return base, None
    extra = regime.extend(months_scn - months_base, seed + 1) if regime is not None else None
    if extra is None:
        extra = np.random.default_rng(seed + 1).integers(0, n_blocks, size=(n_paths, months_scn - months_base))
    return base, extra


def _outcome(stats: gq.PathStats, goal: float | None) -> Outcome:
    from egoquant.report import _chance, _in_100
    final = stats.final
    p = float(np.mean(final >= goal)) if goal else None
    return Outcome(goal_frequency=_in_100(p) if p is not None else None, goal_chance=_chance(p) if p is not None else None,
                   median_final=round(float(np.percentile(final, 50)), 2),
                   p5_final=round(float(np.percentile(final, 5)), 2),
                   drawdown_p95=round(float(np.percentile(stats.drawdowns, 95)), 4))


def evaluate(daily, base_weights: dict[str, float], scn_weights: dict[str, float], fees: dict[str, float],
             base_spec: gq.GoalSpec, scn_spec: gq.GoalSpec, cfg: AgentConfig, n_paths: int = WHAT_IF_PATHS,
             regime=None) -> tuple[Outcome, Outcome]:
    """Baseline and scenario on the SAME simulated futures (common random numbers). regime: the base report's
    sampler (egoquant.regime decision) or None for the block bootstrap."""
    cols = list(daily.columns)
    blocks = gq.block_returns(daily, cfg.goal_block_days)
    fee_v = np.array([fees.get(a, 0.0) for a in cols])
    d_base, extra = _draws(len(blocks), n_paths, base_spec.months, scn_spec.months, cfg.goal_seed, regime)
    d_scn = d_base[:, :scn_spec.months] if extra is None else d_base          # a view, no copy
    # streamed month by month (goal.stream_wealth): no wealth matrices, the same numbers
    wb = gq.stream_wealth(blocks, np.array([base_weights.get(a, 0.0) for a in cols]), fee_v, base_spec, d_base)
    ws = gq.stream_wealth(blocks, np.array([scn_weights.get(a, 0.0) for a in cols]), fee_v, scn_spec, d_scn, extra)
    return _outcome(wb, base_spec.goal_amount), _outcome(ws, scn_spec.goal_amount)


def risk_check(weights: dict[str, float], base: dict, assets: list[AssetInfo], r_levels: dict[str, int],
               as_of: date) -> RiskVerdict:
    """The Risk Engine's independent review of the scenario portfolio against the BASE portfolio's hard limits
    (questionnaire, risk class, Agent B): a scenario can never be checked against looser limits."""
    from egoquant.risk_engine import RiskEngine
    engine = RiskEngine(catalog=[SimpleNamespace(ticker=t, r_level=r) for t, r in r_levels.items()])
    proposal = PortfolioProposal(stage="constrained", method="what_if", as_of=as_of, status="optimal",
                                 weights=weights, engine_version=ENGINE_VERSION)
    constraints = [Constraint(**c) for c in base["hard_constraints"]]
    review = engine.review(proposal, assets, constraints, base.get("max_r_level"))
    from egoquant.risk_engine import plain_violations
    return RiskVerdict(approved=review.approved, reasons=plain_violations(list(review.violations)))


# ==========================================================================================================
# Running one scenario
# ==========================================================================================================
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = defaultdict(deque)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="what-if")


def context_hash(token: str) -> str:
    """A short fingerprint of a sealed context (audit rows and anonymous rate limits; never the content)."""
    return hashlib.sha256((token or "").encode()).hexdigest()[:16]


def rate_limit_key(base: dict, token: str, user_id: str | None) -> str:
    return f"user:{user_id}" if user_id else "ctx:" + context_hash(token)


def check_rate(key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW_S:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_RUNS:
            return False
        hits.append(now)
        return True


def run_what_if(token: str | None, args: Any, *, user_id: str | None = None, account_token: str | None = None,
                timeout_s: float = WHAT_IF_TIMEOUT_S, config: AgentConfig | None = None) -> WhatIfResult:
    """Validate and run one scenario. Never raises: every problem becomes status invalid / unavailable.

    user_id: the signed-in user (None when anonymous). A context bound to a user only runs for that user.
    account_token: the user's Supabase token, needed only to reload saved trades for reoptimize.
    """
    cfg = config or AgentConfig()
    try:
        base = open_what_if_context(token)
        if base.get("user_id") and base["user_id"] != user_id:
            raise WhatIfInvalid("This portfolio belongs to another account. Sign in with that account or rebuild "
                                "your own portfolio.")
        sc = validate_scenario(args, base)
        if not check_rate(rate_limit_key(base, token or "", user_id)):
            return WhatIfResult(status="unavailable", reason="Too many what-if runs. Please wait a few minutes.")
    except WhatIfInvalid as err:
        return WhatIfResult(status="invalid", reason=err.reason)
    future = _executor.submit(_run, base, sc, cfg, user_id, account_token)
    try:
        result = future.result(timeout=timeout_s)
        result.base_report_id = base.get("report_id")
        return result
    except FutureTimeout:
        logger.warning("what-if timed out after %.0f s (mode %s)", timeout_s, sc.mode)
        return WhatIfResult(status="unavailable", reason="The simulation took too long. Please try again in a moment.",
                            scenario=sc.model_dump())
    except WhatIfInvalid as err:
        return WhatIfResult(status="invalid", reason=err.reason, scenario=sc.model_dump())
    except HTTPException as err:                         # the /portfolio code refused the reoptimised inputs
        if 400 <= err.status_code < 500:
            return WhatIfResult(status="invalid", scenario=sc.model_dump(),
                                reason="The engine could not build a portfolio for this scenario within your limits.")
        logger.warning("what-if reoptimize failed: HTTP %s", err.status_code)
        return WhatIfResult(status="unavailable", reason="Market data is unavailable right now. Please try again.",
                            scenario=sc.model_dump())
    except Exception:                                    # never crash the chat; the cause is logged, not shown
        logger.exception("what-if run failed")
        return WhatIfResult(status="unavailable", reason="The simulation could not run right now. Please try again.",
                            scenario=sc.model_dump())


def _run(base: dict, sc: WhatIfScenario, cfg: AgentConfig, user_id: str | None,
         account_token: str | None) -> WhatIfResult:
    as_of = date.fromisoformat(base["as_of"])
    base_spec, scn_spec = _specs(base, sc)
    base_weights = dict(base["weights"])
    classes = dict(base.get("classes") or {})
    assets = [AssetInfo(**a) for a in base.get("assets") or []]
    r_levels = dict(base.get("r_levels") or {})
    fees = dict(base.get("fees") or {})
    selection_paths = None

    if sc.mode == "same_portfolio":
        scn_weights = _renormalise(base_weights, _excluded_tickers(sc, classes, base_weights))
    else:
        scn_weights, extra_assets, extra_classes, extra_r, extra_fees = _reoptimize(base, sc, scn_spec, cfg, account_token)
        assets_by = {a.asset_id: a for a in assets} | {a.asset_id: a for a in extra_assets}
        assets = [assets_by[t] for t in sorted(set(base_weights) | set(scn_weights)) if t in assets_by]
        classes |= extra_classes
        r_levels |= extra_r
        fees |= extra_fees
        selection_paths = WHAT_IF_SELECTION_PATHS
        leaked = _excluded_tickers(sc, classes, scn_weights) & set(scn_weights)
        if leaked:
            return WhatIfResult(status="blocked", reason="The re-optimised portfolio still held something you asked "
                                "to leave out, so it is not shown.", scenario=sc.model_dump(),
                                scenario_text=describe(sc, base))

    verdict = risk_check(scn_weights, base, [a for a in assets if a.asset_id in scn_weights], r_levels, as_of)
    common = dict(scenario=sc.model_dump(), scenario_text=describe(sc, base), risk_engine=verdict, seed=cfg.goal_seed,
                  selection_paths=selection_paths, goal_amount=scn_spec.goal_amount,
                  horizon_years=scn_spec.horizon_years,
                  portfolio_changed=_changed(base_weights, scn_weights))
    if not verdict.approved:
        return WhatIfResult(status="blocked", reason="The Risk Engine rejected the scenario's portfolio: it would "
                            "break one of your limits. No chance is shown for a portfolio that may not be offered.",
                            **common)
    columns = sorted(set(base.get("goal_assets") or []) | set(base_weights) | set(scn_weights))
    daily, _ = goal_history(base, columns, cfg)
    # The base report's own sampler (None = block bootstrap, also when the report has no sampler record).
    regime = regime_hook.decide(daily.index, cfg.goal_block_days, as_of, base.get("data_source"),
                                forced=base.get("sampler") or {"sampler": regime_hook.SAMPLER_BOOTSTRAP})
    b, s = evaluate(daily, base_weights, scn_weights, fees, base_spec, scn_spec, cfg, regime=regime)
    sampler = regime.record() if regime is not None else None
    return WhatIfResult(status="ok", baseline=b, scenario_outcome=s, baseline_goal_frequency=b.goal_frequency,
                        scenario_goal_frequency=s.goal_frequency, n_paths=WHAT_IF_PATHS,
                        new_weights={t: round(w, 6) for t, w in sorted(scn_weights.items(), key=lambda kv: -kv[1])}
                        if common["portfolio_changed"] else None,
                        engine_version=regime_hook.engine_version(sampler, ENGINE_VERSION,
                                                                  regime_hook.WHAT_IF_ENGINE_VERSION),
                        sampler=sampler, **common)


def _changed(a: dict[str, float], b: dict[str, float]) -> bool:
    keys = set(a) | set(b)
    return any(abs(a.get(k, 0.0) - b.get(k, 0.0)) > 1e-6 for k in keys)


def _reoptimize(base: dict, sc: WhatIfScenario, scn_spec: gq.GoalSpec, cfg: AgentConfig, account_token: str | None):
    """Agent A -> Agent B -> Monte Carlo choice with the changed inputs, through the /portfolio route's own code
    (same profile, preferences, game, Crash Test; no news briefing; fewer selection paths)."""
    import egoquant_routes as routes
    if base.get("ibkr") == "ibkr_report" or base.get("ibkr") == "ibkr_flex":
        raise WhatIfInvalid("Re-optimising needs the trading history this portfolio was built from, which is not "
                            "kept. Use the same portfolio, or rebuild with your history connected to your account.")
    stored = None
    if base.get("ibkr") == "account":
        if not account_token:
            raise WhatIfInvalid("Sign in to re-optimise a portfolio built from your saved trading history.")
        from egoquant.accounts.ibkr_input import rows_to_report
        from egoquant.accounts.store import get_store
        store = get_store()
        user = store.user(account_token)
        own = {"user_id": f"eq.{user.id}", "order": "trade_date.asc"}
        trades = store.select(account_token, "trades", own)
        flows = store.select(account_token, "cash_flows", {"user_id": f"eq.{user.id}", "order": "day.asc"}) if trades else []
        stored = (rows_to_report(trades, flows), "account") if trades else None
    inputs = dict(base["inputs"])
    inputs.update(goal_amount=scn_spec.goal_amount, monthly_flow=scn_spec.monthly_flow, horizon_years=scn_spec.horizon_years)
    body = routes.PortfolioIn(**inputs)
    universe_classes = dict(base.get("classes") or {})
    excluded = sorted(_excluded_tickers(sc, universe_classes, universe_classes))
    sel_cfg = cfg.model_copy(update={"goal_paths": WHAT_IF_SELECTION_PATHS})
    with regime_hook.forced_sampler(base.get("sampler")):      # Agent A chooses with the base report's sampler
        _, as_of, run, *_ = routes._execute(body, stored, briefing=False, agent_config=sel_cfg, extra_excluded=excluded,
                                            goal_start=scn_spec.start_amount, as_of=date.fromisoformat(base["as_of"]))
    if not run.approved:
        raise WhatIfInvalid("No re-optimised portfolio passed the Risk Engine for this scenario.")
    weights = {a: w for a, w in run.result.final.weights.items() if w > 1e-9}
    snap = run.agent_c.get_market_snapshot(run.selection.tickers, as_of, sel_cfg.lookback_days).assets
    inst = run.agent_c.instruments
    assets = [a for a in snap if a.asset_id in weights]
    return (weights, assets, {t: inst[t].asset_class for t in weights if t in inst},
            {t: inst[t].r_level for t in weights if t in inst}, {a.asset_id: float(a.expense_ratio) for a in snap})


def audit_summary(result: WhatIfResult) -> dict:
    """What the audit log keeps: no amounts beyond the scenario itself, no weights, no personal data."""
    return {"status": result.status, "baseline": result.baseline_goal_frequency,
            "scenario": result.scenario_goal_frequency, "portfolio_changed": result.portfolio_changed,
            "risk_approved": result.risk_engine.approved if result.risk_engine else None}


__all__ = ["WhatIfInvalid", "WhatIfResult", "WhatIfScenario", "build_what_if_facts", "open_what_if_context",
           "run_what_if", "seal_what_if_context", "validate_scenario"]
