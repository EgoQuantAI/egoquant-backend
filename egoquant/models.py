"""Shared data contracts for EgoQuant, version 3 (PROPOSED shared models).

This file is shared between teammates. Treat every class here as a proposal until the
team agrees on it. Agent A only needs these shapes to talk to:

    Risk questionnaire -> InvestorContext.risk_class  (C1..C5, already computed upstream)
    Advisor chatbot    -> InvestorContext             (validated numbers, never raw chat text)
    Agent C            -> MarketSnapshot              (prices + asset metadata)
    Agent A            -> PortfolioMenu               (candidate portfolios, sent to Agent B)
    Agent B            -> list[Constraint]            (numeric limits, never a chosen portfolio)
    Agent A            -> PortfolioProposal           (final proposal, checked by the Risk Engine)

What changed in version 3:
    * Two new asset classes Agent A can hold: "commodity" (gold, silver, broad commodities) and
      "crypto" (bitcoin ETP, Binance spot pairs).
    * New constraint type "max_class_weight" (with `asset_class`): a cap on the total weight of one class.
    * RiskClassPolicy gained max_commodity and max_crypto (from the questionnaire's C-level table) and
      max_cash (Agent A's own cap, so a growth portfolio does not park its non-equity part in T-bills).

Version 2 introduced `risk_class` (C1..C5) instead of a score, RiskClassPolicy, "max_turnover", the
"ledoit_wolf" estimator, the dynamic risk budget, stressed-correlation volatility and reverse stress.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, get_args

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# Small vocabularies (Literals, so typos fail loudly at validation time)
# --------------------------------------------------------------------------
AssetClass = Literal["equity", "bond", "cash", "commodity", "crypto"]
ASSET_CLASSES: tuple[str, ...] = get_args(AssetClass)
RiskClass = Literal["C1", "C2", "C3", "C4", "C5"]   # C1 = lowest risk tolerance, C5 = highest
ConstraintType = Literal[
    "max_equity", "max_asset_weight", "min_cash", "max_vol", "max_stress_loss", "max_turnover", "max_class_weight",
    "min_class_weight",
]
ConstraintSource = Literal["risk_class", "suitability", "investor_preference", "system", "behavioral"]
ProposalStatus = Literal["optimal", "fallback", "infeasible"]
ProposalStage = Literal["baseline", "optimized", "constrained"]
CovEstimator = Literal["sample", "ewma", "ledoit_wolf"]
RISK_CLASS_ORDER: list[str] = ["C1", "C2", "C3", "C4", "C5"]


# --------------------------------------------------------------------------
# Risk class (C1..C5) -> numeric portfolio limits
# --------------------------------------------------------------------------
class RiskClassPolicy(BaseModel):
    """What one questionnaire class means in numbers. Hard limits plus a default equity level."""

    risk_class: RiskClass
    max_equity: float = Field(ge=0, le=1)         # hard cap on the equity weight
    target_equity: float = Field(ge=0, le=1)      # default equity level for this class (<= max_equity)
    max_vol: float = Field(gt=0)                  # hard cap on annualized portfolio volatility
    max_stress_loss: float = Field(gt=0, le=1)    # hard cap on loss in AgentConfig.capacity_scenario
    max_commodity: float = Field(0.0, ge=0, le=1)  # hard cap on gold and other commodities
    max_crypto: float = Field(0.0, ge=0, le=1)     # hard cap on crypto-assets
    max_cash: float = Field(1.0, gt=0, le=1)       # hard cap on cash-like funds; raised to the
                                                   # investor's own cash need when that is larger
    # Portfolio method v2 only (EGOQUANT_PORTFOLIO_METHOD=v2, docs/PORTFOLIO_V2.md): the most one fund may hold. None = the
    # investor's max_asset_weight as before (0.40). PROVISIONAL; decided by Savas on 19 Oct (30% for C3 to C5).
    v2_max_fund_weight: float | None = Field(None, gt=0, le=1, exclude=True)    # exclude: v1 fingerprints stay identical

    @model_validator(mode="after")
    def _target_within_cap(self) -> "RiskClassPolicy":
        if self.target_equity > self.max_equity:
            raise ValueError("target_equity cannot exceed max_equity")
        return self


# max_equity, max_stress_loss, max_commodity and max_crypto come from the risk questionnaire's C-level
# table (egoquant/risk_profile/config.yaml: c_levels), which is the source of truth;
# tests/risk_profile/test_risk_profile.py checks they stay in sync. target_equity, max_vol and max_cash
# are Agent A's own placeholders. NOT the bank's policy: all numbers are PROVISIONAL.
DEFAULT_RISK_CLASS_POLICIES: dict[str, RiskClassPolicy] = {
    "C1": RiskClassPolicy(risk_class="C1", max_equity=0.00, target_equity=0.00, max_vol=0.04, max_stress_loss=0.05,
                          max_commodity=0.00, max_crypto=0.00, max_cash=1.00),
    "C2": RiskClassPolicy(risk_class="C2", max_equity=0.00, target_equity=0.00, max_vol=0.07, max_stress_loss=0.10,
                          max_commodity=0.00, max_crypto=0.00, max_cash=1.00),
    "C3": RiskClassPolicy(risk_class="C3", max_equity=0.40, target_equity=0.40, max_vol=0.10, max_stress_loss=0.20,
                          max_commodity=0.10, max_crypto=0.00, max_cash=0.30, v2_max_fund_weight=0.30),
    "C4": RiskClassPolicy(risk_class="C4", max_equity=0.70, target_equity=0.60, max_vol=0.13, max_stress_loss=0.35,
                          max_commodity=0.15, max_crypto=0.00, max_cash=0.20, v2_max_fund_weight=0.30),
    "C5": RiskClassPolicy(risk_class="C5", max_equity=0.90, target_equity=0.80, max_vol=0.17, max_stress_loss=0.45,
                          max_commodity=0.20, max_crypto=0.05, max_cash=0.15, v2_max_fund_weight=0.30),
}

# Asset classes with a per-class cap in RiskClassPolicy, and the field that holds each cap.
CLASS_CAP_FIELDS: dict[str, str] = {"commodity": "max_commodity", "crypto": "max_crypto", "cash": "max_cash"}


# --------------------------------------------------------------------------
# Inputs: risk questionnaire + Advisor chatbot -> Agent A
# --------------------------------------------------------------------------
class PortfolioView(BaseModel):
    """A small, deterministic tilt for portfolio method v2 (Black-Litterman view), built from the investor's interests and
    preferences by egoquant/preferences/views.py. Never a forecast: `annual_excess` is an assumption in return per year."""

    kind: Literal["area", "sector", "region", "asset"]
    key: str = Field(min_length=1, max_length=40)
    annual_excess: float = Field(ge=-0.02, le=0.02)       # at most +-2% a year
    confidence: float = Field(gt=0, le=1)


class InvestorContext(BaseModel):
    """Financial facts and the questionnaire class, already extracted and validated upstream.

    Three kinds of risk information stay SEPARATE on purpose:
      * questionnaire class -> risk_class (C1..C5), turned into limits by RiskClassPolicy
      * capacity            -> max_acceptable_loss, liquidity_need, min_cash_buffer, horizon_years
      * revealed tolerance  -> comes later from Agent B as Constraint objects
    """

    investor_id: str
    risk_class: RiskClass                            # output of the risk assessment questionnaire
    goal_type: str                                   # e.g. "retirement", "house", "growth"
    horizon_years: float = Field(gt=0)
    base_currency: str = "USD"

    # Capacity (what the investor can afford)
    liquidity_need: float = Field(0.0, ge=0, le=1)   # share of wealth possibly needed soon
    min_cash_buffer: float = Field(0.0, ge=0, le=1)  # minimum share held in cash-like assets
    max_acceptable_loss: float | None = Field(None, ge=0, le=1)  # fraction, e.g. 0.25 = 25%; 0 = no loss accepted

    # Regulatory / suitability cap that applies on top of the risk class
    suitability_max_equity: float = Field(1.0, ge=0, le=1)

    # Universe and implementation preferences
    excluded_assets: list[str] = Field(default_factory=list)
    max_asset_weight: float = Field(0.40, gt=0, le=1)
    max_turnover: float | None = Field(None, ge=0, le=1)   # one-way turnover limit versus current holdings

    # Current portfolio (optional): used for turnover, costs and trade lists
    current_holdings: dict[str, float] | None = None

    # Assets that come from the investor's chosen interest areas (Agent C's selection). Used only by the
    # goal-based path's "interest tilt" candidates; assets outside the universe are ignored.
    interest_assets: list[str] = Field(default_factory=list)

    # Portfolio method v2 only (docs/PORTFOLIO_V2.md). exclude=True keeps it out of model_dump_json(), so the input
    # fingerprints of the default engine (v1) do not change; v2 mixes these views into its own seed.
    views: list[PortfolioView] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _holdings_are_weights(self) -> "InvestorContext":
        if self.current_holdings is not None:
            values = list(self.current_holdings.values())
            if any(v < 0 for v in values):
                raise ValueError("current_holdings must be non-negative weights")
            if abs(sum(values) - 1.0) > 1e-3:
                raise ValueError("current_holdings must be weights that sum to 1")
        return self


class AssetInfo(BaseModel):
    """Static metadata about one investable asset (supplied by Agent C)."""

    asset_id: str
    asset_class: AssetClass
    currency: str = "USD"
    expense_ratio: float = Field(0.0, ge=0)           # annual fee, fraction
    trading_cost_bps: float = Field(0.0, ge=0)        # one-way cost in basis points
    duration_years: float | None = None               # bonds only; used by rate-shock scenarios
    convexity: float | None = None                    # bonds only; second-order rate effect
    liquidity_tier: int = 1                           # 1 = most liquid
    max_weight: float | None = Field(None, gt=0, le=1)  # instrument cap set by Agent C (e.g. single stocks)
    # Metadata for portfolio method v2 (docs/PORTFOLIO_V2.md): screening and group caps. All optional; Agent C fills them.
    # exclude=True keeps them out of model_dump(), so the input fingerprints of the default engine (v1) do not change.
    area: str | None = Field(None, exclude=True)      # the catalog's interest area (e.g. "us_broad", "gold")
    region: str | None = Field(None, exclude=True)    # e.g. "US", "developed ex-US", "emerging"
    sector: str | None = Field(None, exclude=True)    # stocks and sector funds
    history_start: date | None = Field(None, exclude=True)   # first price date available
    product_type: str | None = Field(None, exclude=True)     # e.g. "etf", "stock", "crypto"


class MarketSnapshot(BaseModel):
    """Prices and metadata from Agent C. Nothing after `as_of` may be used."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    as_of: date
    prices: pd.DataFrame                              # index = dates, columns = asset_id, ADJUSTED prices
    assets: list[AssetInfo]
    risk_free_rate_annual: float = 0.0
    data_source: Literal["synthetic", "historical"] = "synthetic"


# --------------------------------------------------------------------------
# Constraints: the common language between suitability rules, Agent B and Agent A
# --------------------------------------------------------------------------
class Constraint(BaseModel):
    """One numeric limit on a portfolio, with its origin kept explicit."""

    type: ConstraintType
    value: float
    source: ConstraintSource
    hard: bool = True                                 # soft constraints may be relaxed if infeasible
    confidence: float | None = Field(None, ge=0, le=1)  # mainly for behavioral constraints
    evidence_ref: str | None = None                   # pointer to Agent B's evidence
    scenario: str | None = None                       # required for max_stress_loss
    asset_id: str | None = None                       # optional for max_asset_weight
    asset_class: AssetClass | None = None             # required for max_class_weight / min_class_weight

    @model_validator(mode="after")
    def _required_fields(self) -> "Constraint":
        if self.type == "max_stress_loss" and not self.scenario:
            raise ValueError("max_stress_loss constraints must name a scenario")
        if (self.type in ("max_class_weight", "min_class_weight")) != (self.asset_class is not None):
            raise ValueError("asset_class is required for max_class_weight / min_class_weight and only allowed there")
        return self

    @property
    def label(self) -> str:
        """Readable id used in the 'binding', 'relaxed', 'ignored' and 'violated' lists."""
        tag = f"{self.source}:{self.type}"
        if self.scenario:
            tag += f"[{self.scenario}]"
        if self.asset_class:
            tag += f"[{self.asset_class}]"
        if self.asset_id:
            tag += f"[{self.asset_id}]"
        return tag


class StressScenario(BaseModel):
    """A HYPOTHETICAL market shock. Not a forecast. Edit freely with the team."""

    name: str
    description: str
    asset_class_shocks: dict[AssetClass, float] = Field(default_factory=dict)  # e.g. {"equity": -0.30}
    rate_shock_bp: float = 0.0       # parallel rate move; bond return = -duration*dy + 0.5*convexity*dy^2


# --------------------------------------------------------------------------
# Configuration for Agent A (makes every run reproducible)
# --------------------------------------------------------------------------
class AgentConfig(BaseModel):
    engine_version: str = "0.3.0"

    # Universe and estimation
    universe: list[str] | None = None                 # None = ask Agent C for its approved universe
    lookback_days: int = Field(504, ge=30)            # rows of history requested from Agent C
    window_days: int = Field(252, ge=30)              # rows actually used for the estimates
    cov_estimator: CovEstimator = "ledoit_wolf"
    ewma_decay: float = Field(0.94, gt=0, lt=1)       # lambda in the EWMA recursion
    shrinkage: float = Field(0.10, ge=0, le=1)        # fixed intensity, used by "sample" and "ewma" only
    periods_per_year: int = 252                       # daily data

    # Objective
    objective: Literal["min_variance", "mean_variance"] = "min_variance"
    risk_aversion: float = Field(5.0, gt=0)           # lambda in mean-variance utility
    expected_returns: dict[str, float] | None = None  # ANNUAL ASSUMPTIONS (gross of fees), never historical means

    # Risk class policy (C1..C5 -> limits)
    risk_class_policies: dict[RiskClass, RiskClassPolicy] = Field(
        default_factory=lambda: dict(DEFAULT_RISK_CLASS_POLICIES)
    )
    capacity_scenario: str = "equity_down_30_bond_down_5"   # scenario used for loss-limit constraints

    # Baseline portfolio (naive reference). None = class target equity, remainder in bonds.
    baseline_class_mix: dict[str, float] | None = None

    # Candidate menu
    menu_size: int = Field(10, ge=2, le=25)
    menu_min_equity: float = Field(0.0, ge=0, le=1)

    # Goal-based path: candidate portfolios (levels x construction styles) for Agent B to screen
    candidate_levels: int = Field(20, ge=2, le=50)         # points on the risk axis (stock share, or bond share at 0% stocks)
    candidate_target: int = Field(100, ge=10, le=300)      # distinct candidates to build (more levels are added if needed)
    # PROVISIONAL: an investor who chose crypto (and may hold it) holds at least this much, never above the class
    # cap. Soft: a hard limit (stress loss, Agent B) wins and the floor is reported as relaxed.
    interest_crypto_floor: float = Field(0.02, ge=0, le=1)
    candidate_ridge: float = Field(1.0, ge=0)              # "diversified" style: penalty, in units of the average asset variance
    candidate_interest_tilt: float = Field(3.0, ge=1)      # "interest tilt" style: weight multiple for interest assets
    candidate_min_distance: float = Field(0.005, ge=0)     # two candidates closer than this (max weight gap) are one

    # Goal-based path: Monte Carlo of the approved candidates (block bootstrap of past returns)
    # ~20 years of daily prices: the sample must hold a full cycle incl. a severe bear market (2008-09); ten years
    # (2017-2026, one of the strongest US decades) made almost any goal look certain. Younger funds are completed
    # by goal.complete_history. One request per ticker returns any range, so this costs no extra requests.
    goal_history_days: int = Field(5040, ge=126)
    goal_paths: int = Field(10_000, ge=500, le=100_000)
    goal_block_days: int = Field(21, ge=1)                 # resample whole ~1-month blocks to keep crash clustering
    goal_seed: int = 20261017                              # fixed seed: same inputs, same simulation
    goal_tie_tolerance: float = Field(0.01, ge=0, le=0.2)  # goal probabilities this close count as a tie
    # PROVISIONAL: below ~5 years of history (after completing short ones) the simulation would just replay one
    # market phase for decades (e.g. two bull years -> "100% chance"), so no goal probabilities are shown.
    goal_min_history_days: int = Field(1260, ge=126)
    # Equities and crypto were picked from today's most traded list (survivorship / look-ahead bias): keep their
    # real daily moves but set their average return to beta x the broad market's (goal.equilibrium_means).
    goal_hindsight_correction: bool = True

    # Dynamic risk budgeting (volatility regime). It only ever TIGHTENS the volatility cap.
    dynamic_risk_budget: bool = True
    regime_low: float = Field(0.8, gt=0)              # EWMA vol / long-run vol below this = "low"
    regime_high: float = Field(1.25, gt=0)            # above this = "high"; budget shrinks beyond it
    min_risk_budget_scale: float = Field(0.5, gt=0, le=1)  # floor on the budget shrink factor

    # Final proposal search
    equity_step_down: float = Field(0.01, gt=0, le=0.25)   # equity decrement while hunting for feasibility

    # Implementation costs (final proposal only; needs current holdings)
    turnover_penalty: float = Field(0.0, ge=0)        # weight on trading cost in the objective (0 = off)

    # Risk statistics
    var_confidence: float = Field(0.95, gt=0, lt=1)
    stress_correlation: float = Field(0.90, ge=0, le=1)    # correlation floor in the stressed-correlation test

    # Behavioral integration
    min_behavioral_confidence: float = Field(0.5, ge=0, le=1)  # weaker evidence is ignored, explicitly

    # Sensitivity to estimation choices
    run_sensitivity: bool = True
    sensitivity_windows: list[int] = Field(default_factory=lambda: [63, 126, 252])
    sensitivity_estimators: list[CovEstimator] = Field(default_factory=lambda: ["sample", "ewma", "ledoit_wolf"])

    @model_validator(mode="after")
    def _check_consistency(self) -> "AgentConfig":
        if self.window_days > self.lookback_days:
            raise ValueError("window_days cannot exceed lookback_days")
        if not self.regime_low < self.regime_high:
            raise ValueError("regime_low must be smaller than regime_high")
        policies = self.risk_class_policies
        if set(policies) != set(RISK_CLASS_ORDER):
            raise ValueError("risk_class_policies must define exactly C1, C2, C3, C4 and C5")
        for key, policy in policies.items():
            if policy.risk_class != key:
                raise ValueError(f"policy stored under {key} says it is {policy.risk_class}")
        for field_name in ("max_equity", "max_vol", "max_stress_loss", "max_commodity", "max_crypto"):
            values = [getattr(policies[k], field_name) for k in RISK_CLASS_ORDER]
            if values != sorted(values):
                raise ValueError(f"{field_name} must not decrease from C1 to C5")
        cash_caps = [policies[k].max_cash for k in RISK_CLASS_ORDER]
        if cash_caps != sorted(cash_caps, reverse=True):
            raise ValueError("max_cash must not increase from C1 to C5")
        return self


# --------------------------------------------------------------------------
# Outputs: Agent A -> Agent B / Risk Engine / Advisor
# --------------------------------------------------------------------------
class DataQualityReport(BaseModel):
    passed: bool
    issues: list[str]                                 # problems that block the run
    warnings: list[str]                               # suspicious but not blocking
    n_observations: int
    first_date: date | None
    last_date: date | None


class RiskMetrics(BaseModel):
    """All values are HISTORICAL IN-SAMPLE ESTIMATES or HYPOTHETICAL tests, never forecasts."""

    ex_ante_vol: float                                # annualized sqrt(w' Sigma w)
    stressed_corr_vol: float                          # same, with risky-asset correlations raised (hypothetical)
    risk_contributions: dict[str, float]              # share of portfolio variance per asset (sums to 1)
    diversification_ratio: float                      # weighted avg asset vol / portfolio vol
    effective_n_holdings: float                       # 1 / sum(w^2)
    hist_var: float                                   # 1-day historical VaR (positive = loss)
    hist_cvar: float                                  # 1-day historical expected shortfall
    n_tail_obs: int                                   # observations behind the CVaR (small = fragile)
    hist_max_drawdown: float                          # constant-weight, in-sample window
    var_confidence: float
    label: str = "historical in-sample estimates and hypothetical tests; not forecasts"


class ImplementationPlan(BaseModel):
    turnover: float                                   # 0.5 * sum |new - current|
    estimated_cost: float                             # fraction of portfolio value
    trades: dict[str, float]                          # signed weight change per asset


class SensitivityRow(BaseModel):
    window_days: int
    estimator: str
    max_abs_weight_change: float                      # versus the main estimate, same constraints
    ex_ante_vol: float


class PortfolioProposal(BaseModel):
    stage: ProposalStage
    method: str
    as_of: date
    risk_class: RiskClass | None = None
    status: ProposalStatus
    message: str = ""
    weights: dict[str, float]                         # empty when infeasible
    equity_weight: float | None = None
    risk: RiskMetrics | None = None
    stress: dict[str, float] = Field(default_factory=dict)  # scenario -> HYPOTHETICAL portfolio return
    reverse_stress_equity_drop: float | None = None   # equity fall (others flat) that would hit the loss limit
    binding_constraints: list[str] = Field(default_factory=list)
    relaxed_constraints: list[str] = Field(default_factory=list)   # soft constraints dropped to find a solution
    ignored_constraints: list[str] = Field(default_factory=list)   # behavioral constraints below confidence floor
    violated_constraints: list[str] = Field(default_factory=list)  # empty for optimizer output; may be non-empty for the naive baseline
    limiting_constraints: list[str] = Field(default_factory=list)  # limits that stopped a higher equity level (set only when it was lowered)
    implementation: ImplementationPlan | None = None
    sensitivity: list[SensitivityRow] = Field(default_factory=list)
    diagnostics: dict[str, float | str] = Field(default_factory=dict)
    data_source: str = "synthetic"
    vol_estimator: str = ""
    input_hash: str = ""
    engine_version: str = ""


class PortfolioMenu(BaseModel):
    """What Agent A sends to Agent B: comparable portfolios along one risk axis."""

    as_of: date
    risk_class: RiskClass
    risk_axis: Literal["equity_target"] = "equity_target"
    target_equity: float                              # the class default level (capped by every equity limit)
    baseline: PortfolioProposal                       # naive fixed-weight reference
    portfolios: list[PortfolioProposal]               # ordered from low to high equity
    default_index: int | None                         # highest menu level not above the class target
    infeasible_levels: list[float] = Field(default_factory=list)
    data_quality: DataQualityReport
    input_hash: str


class AgentAResult(BaseModel):
    """Everything produced by one full run (menu -> Agent B -> constrained proposal)."""

    menu: PortfolioMenu
    behavioral_constraints: list[Constraint]
    final: PortfolioProposal


# --------------------------------------------------------------------------
# Interfaces for the other agents (Agent A depends on these shapes only)
# --------------------------------------------------------------------------
class MarketDataProvider(Protocol):
    """Agent C. Agent A asks for data; it never scrapes or fetches by itself."""

    def get_market_snapshot(
        self, asset_ids: list[str] | None, as_of: date, lookback_days: int
    ) -> MarketSnapshot: ...


class BehavioralAdvisor(Protocol):
    """Agent B. Receives the menu, returns NUMERIC constraints (never a chosen portfolio)."""

    def evaluate_menu(self, investor_id: str, menu: PortfolioMenu) -> list[Constraint]: ...
