"""Data contracts owned by Agent B (the Behavioral Twin).

These live in their own file on purpose: egoquant/models.py is shared and changes only by
team agreement. Agent B talks to Agent A only through the shared types there:

    Agent A  -> PortfolioMenu       -> Agent B
    Agent B  -> list[Constraint]    -> Agent A     (source="behavioral", numeric only)

Everything else in this file is B's own input (trading logs, the Eckel-Grossman game result)
and B's own output for the Advisor and the audit trail (BehavioralReport).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from egoquant.models import DEFAULT_RISK_CLASS_POLICIES, AssetClass, Constraint, RiskClass, RiskClassPolicy
from egoquant.scenarios import CAPACITY_SCENARIO

EvidenceSource = Literal["real", "game"]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
class Trade(BaseModel):
    """One executed trade inside the account. Deposits and withdrawals are NOT trades."""

    trade_date: date
    asset_id: str
    side: Literal["buy", "sell"]
    value: float = Field(gt=0)                       # traded amount in base currency


class AssetFundamentals(BaseModel):
    """Tags for one traded asset, used ONLY to spot hype buys. Synthetic for the demo; Agent C later."""

    instrument: Literal["fund", "stock", "other"] = "fund"
    profitable: bool | None = None                   # positive trailing earnings
    debt_to_equity: float | None = Field(None, ge=0)


class TradingHistory(BaseModel):
    """What a brokerage export gives: daily position values, cash flows and trades.

    positions        index = dates (DatetimeIndex, ascending, unique, timezone-naive),
                     columns = asset_id, values = END-OF-DAY market value AFTER that day's trades.
                     Cash must be one of the columns, so selling equity into cash keeps the
                     account value unchanged.
    external_flows   index = dates, + deposit / - withdrawal. Only days with a flow need a row.
    asset_classes    class of every column in `positions`.
    asset_prices     OPTIONAL daily prices per asset (same dates as positions). Needed for cost of
                     panic, disposition effect, return chasing and hype chasing; without it those
                     four patterns are reported as not computable.
    fundamentals     OPTIONAL tags per asset for hype chasing.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    positions: pd.DataFrame
    external_flows: pd.Series = Field(default_factory=lambda: pd.Series(dtype=float))
    trades: list[Trade] = Field(default_factory=list)
    asset_classes: dict[str, AssetClass]
    asset_prices: pd.DataFrame | None = None
    fundamentals: dict[str, AssetFundamentals] = Field(default_factory=dict)
    base_currency: str = "USD"


class GameResult(BaseModel):
    """The Eckel-Grossman game (six 50/50 gambles), exactly as specified in the team document."""

    task: Literal["eckel_grossman_6"] = "eckel_grossman_6"
    choice: int = Field(ge=1, le=6)                  # 1 = sure thing ... 6 = risk-seeking
    payoff_low: float
    payoff_high: float
    hypothetical: bool = True                        # no real money at stake
    version: str
    timestamp: str                                   # ISO 8601

    @field_validator("timestamp")
    @classmethod
    def _iso(cls, value: str) -> str:
        datetime.fromisoformat(value)                # raises if it is not ISO 8601
        return value

    @property
    def played_on(self) -> date:
        return datetime.fromisoformat(self.timestamp).date()


class LossAcceptance(BaseModel):
    """The app's Risk Game "loss acceptance" score (fintechaton-frontend src/app/game).

    After each significant losing round, which gamble did the player pick next? 0 = always the safest,
    100 = always the riskiest. Hypothetical (no real money), like the Eckel-Grossman game.
    """

    score: float = Field(ge=0, le=100)
    rounds_after_loss: int | None = Field(None, ge=0)   # losing rounds the score is based on; None = not reported
    timestamp: str                                      # ISO 8601, when the game was played

    @field_validator("timestamp")
    @classmethod
    def _iso(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @property
    def played_on(self) -> date:
        return datetime.fromisoformat(self.timestamp).date()


class CrashTestResult(BaseModel):
    """The app's Crash Test (fintechaton-frontend src/app/game): the player holds a balanced fund through a
    fixed, hypothetical fall-and-recovery path and may sell at every step. A stand-in for panic-selling
    evidence when there is no trading history. Hypothetical (no real money), so it never makes a limit hard.
    """

    sold_at_drawdown: float | None = Field(None, ge=0, le=1)   # loss from the peak at the first sale; None = never sold
    sold_fraction: float = Field(0.0, ge=0, le=1)               # share of the fund sold in total
    max_drawdown_seen: float = Field(ge=0, le=1)                # deepest loss from the peak the player lived through
    timestamp: str                                              # ISO 8601, when the test was played

    @field_validator("timestamp")
    @classmethod
    def _iso(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "CrashTestResult":
        if self.sold_fraction > 0 and self.sold_at_drawdown is None:
            raise ValueError("sold_at_drawdown is required when something was sold")
        if self.sold_at_drawdown is not None and self.sold_at_drawdown > self.max_drawdown_seen + 1e-9:
            raise ValueError("sold_at_drawdown cannot exceed max_drawdown_seen")
        return self

    @property
    def played_on(self) -> date:
        return datetime.fromisoformat(self.timestamp).date()


class BehavioralData(BaseModel):
    """Everything B knows about one investor's behavior."""

    investor_id: str
    history: TradingHistory | None = None
    game_result: GameResult | None = None
    loss_acceptance: LossAcceptance | None = None            # the app's Risk Game; tighten-only, like the game
    crash_test: CrashTestResult | None = None                # the app's Crash Test; tighten-only, like the game
    stated_horizon_years: float | None = Field(None, gt=0)   # from the intake, for horizon mismatch only
    data_source: Literal["real", "synthetic"]


class BehavioralDataProvider(Protocol):
    """Where B gets behavior data. Returns None when the investor has none at all."""

    def get_behavioral_data(self, investor_id: str) -> BehavioralData | None: ...


# --------------------------------------------------------------------------
# Configuration (every threshold is here, so every result is reproducible)
# --------------------------------------------------------------------------
class BehaviorConfig(BaseModel):
    version: str = "b-0.2.0"

    # Class policies: the SAME table Agent A uses (models.DEFAULT_RISK_CLASS_POLICIES). If the
    # orchestrator gives Agent A custom policies, pass the same ones here.
    risk_class_policies: dict[RiskClass, RiskClassPolicy] = Field(
        default_factory=lambda: dict(DEFAULT_RISK_CLASS_POLICIES))
    stress_scenario: str = CAPACITY_SCENARIO              # must equal Agent A's capacity_scenario
    # The Risk Game is part of the risk CATEGORY (egoquant/risk_profile, docs/RISK_GAME_BLEND.md). True = its two scores
    # (game_result, loss_acceptance) are not used here a second time as evidence for a tighter limit; the Crash Test and the
    # trade log still are. False (default) = as before.
    game_in_profile: bool = False

    @model_serializer(mode="wrap")
    def _omit_game_in_profile_when_off(self, handler):
        """Agent B hashes this config into every report's input hash: while the flag is off the config serializes exactly as
        it did before the flag existed, so no hash, evidence reference or golden moves."""
        data = handler(self)
        if not self.game_in_profile:
            data.pop("game_in_profile", None)
        return data

    feasibility_slack: float = Field(1e-5, ge=0)          # margin kept when clamping a limit to the menu
    caution_margin: float = Field(0.15, ge=0, lt=1)       # loss within 15% of a limit -> "caution"

    risky_classes: list[AssetClass] = Field(default_factory=lambda: ["equity"])
    min_history_days: int = Field(126, ge=1)

    # Drawdown measurement
    peak_window_days: int = Field(252, ge=2)
    spell_floor: float = Field(0.03, gt=0, lt=1)

    # Panic selling: net-sold >= X of equity within N days while down >= min_trigger
    min_trigger_drawdown: float = Field(0.05, gt=0, lt=1)
    min_sell_fraction: float = Field(0.30, gt=0, le=1)
    sell_window_days: int = Field(10, ge=1)
    max_withdrawal_share: float = Field(0.5, gt=0, le=1)
    min_informative_depth: float = Field(0.08, gt=0, lt=1)

    # Strength of panic evidence
    consistency_band: float = Field(0.05, ge=0)
    contradiction_margin: float = Field(0.02, ge=0)
    conf_single_episode: float = Field(0.60, ge=0, le=1)
    conf_single_with_game: float = Field(0.70, ge=0, le=1)   # one panic + game agrees ("Sara rule")
    conf_multi_base: float = Field(0.70, ge=0, le=1)
    conf_multi_step: float = Field(0.10, ge=0, le=1)
    conf_multi_cap: float = Field(0.90, ge=0, le=1)
    conf_contradicted: float = Field(0.45, ge=0, le=1)       # below Agent A's 0.5 floor: reported, not enforced

    # Eckel-Grossman game
    game_level_map: dict[int, int] = Field(default_factory=lambda: {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 5})
    game_gap_levels: int = Field(2, ge=1)                    # act only when the game is this many levels lower
    conf_game: float = Field(0.55, ge=0, le=1)
    # Live advice: the game is played at decision time, after the last market close (menu.as_of).
    # False (backtests) ignores a game dated after as_of; the web service sets it True.
    accept_game_after_as_of: bool = False                    # also applies to loss acceptance

    # Loss acceptance from the app's Risk Game (score 0-100), PROVISIONAL. Equal-width bands give a level
    # 1-5 (upper bound of each band -> level). Like the game it only acts with no informative trading
    # history, when its level is game_gap_levels or more below the class, and never makes a limit hard.
    # Scores at or above loss_acceptance_neutral never set a limit: they do not show retreating after a
    # loss (and 50 is what the app reports when no losing round was played).
    loss_acceptance_bands: list[tuple[float, int]] = Field(
        default_factory=lambda: [(20.0, 1), (40.0, 2), (60.0, 3), (80.0, 4), (100.0, 5)])
    loss_acceptance_neutral: float = Field(50.0, ge=0, le=100)
    conf_loss_acceptance: float = Field(0.55, ge=0, le=1)
    conf_game_and_loss_acceptance: float = Field(0.60, ge=0, le=1)   # two or more game signals agree; still soft
    # Crash Test, PROVISIONAL: selling at least min_sell_fraction of the fund marks a hypothetical capitulation
    # point; it acts (soft) when that loss is below the class's own limit and there is no informative history.
    conf_crash_test: float = Field(0.55, ge=0, le=1)
    # Game-type evidence alone (game, loss acceptance, crash test) lowers the loss limit by at most this many
    # classes below the questionnaire class (e.g. C4 -> C3's limit). Real trading history is not capped.
    game_max_class_drop: int = Field(1, ge=1, le=4)

    # Goal-based path: how many candidate portfolios B hands back to Agent A for the simulation
    screen_min_keep: int = Field(5, ge=1)
    screen_max_keep: int = Field(20, ge=1)

    # Overtrading -> soft turnover limit
    overtrading_turnover: float = Field(1.0, gt=0)          # one-way per year
    turnover_limit: float = Field(0.20, gt=0, le=1)          # per rebalance, vs current holdings
    turnover_slack: float = Field(0.02, ge=0)
    conf_turnover: float = Field(0.60, ge=0, le=1)

    # Flags
    high_concentration: float = Field(0.50, gt=0, le=1)
    panic_cost_window_days: int = Field(252, ge=1)            # compare with holding for one year
    panic_cost_flag: float = Field(0.02, ge=0)               # cost above 2% of account value
    disposition_min_sell_days: int = Field(5, ge=1)
    disposition_flag: float = Field(0.10)                    # PGR - PLR above this
    rally_lookback_days: int = Field(20, ge=2)
    rally_percentile: float = Field(0.80, gt=0, lt=1)
    chase_min_buys: int = Field(10, ge=1)
    chase_flag_share: float = Field(0.40, gt=0, le=1)        # 2x the 20% you'd get by chance
    hype_lookback_days: int = Field(20, ge=2)
    hype_spike_return: float = Field(0.25, gt=0)
    weak_debt_to_equity: float = Field(2.0, gt=0)
    hype_flag_min_buys: int = Field(2, ge=1)
    horizon_mismatch_ratio: float = Field(0.5, gt=0, le=1)   # holding period < half the stated horizon
    equity_gap_flag: float = Field(0.15, ge=0, le=1)

    # Diagnostics (ASSUMPTIONS, never used to build portfolios)
    assumed_equity_premium: float = 0.05
    assumed_equity_vol: float = Field(0.16, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "BehaviorConfig":
        if self.min_trigger_drawdown < self.spell_floor:
            raise ValueError("min_trigger_drawdown must be >= spell_floor")
        if sorted(self.game_level_map) != [1, 2, 3, 4, 5, 6] or \
                not all(1 <= v <= 5 for v in self.game_level_map.values()):
            raise ValueError("game_level_map must map choices 1-6 to levels 1-5")
        if set(self.risk_class_policies) != {"C1", "C2", "C3", "C4", "C5"}:
            raise ValueError("risk_class_policies must define C1..C5")
        bands = self.loss_acceptance_bands
        if not bands or [u for u, _ in bands] != sorted(u for u, _ in bands) or bands[-1][0] < 100                 or not all(1 <= lvl <= 5 for _, lvl in bands):
            raise ValueError("loss_acceptance_bands must be ascending upper bounds ending at 100, levels 1-5")
        return self

    def loss_acceptance_level(self, score: float) -> int:
        return next(level for upper, level in self.loss_acceptance_bands if score <= upper)


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
EvidenceKind = Literal["capitulation", "held_through", "game", "loss_acceptance", "crash_test", "hype_buy"]


class Evidence(BaseModel):
    ref: str                                         # e.g. "capitulation:2025-03-31"
    kind: EvidenceKind
    source: EvidenceSource
    start: date
    end: date
    drawdown: float | None = None                    # trigger loss (capitulation) or depth (held_through)
    sold_fraction: float | None = None
    asset_id: str | None = None
    value: float | None = None                       # pattern-specific number (e.g. game level, buy value)
    note: str = ""


FeatureName = Literal[
    "history_days", "capitulation_threshold", "max_drawdown_held_through", "revealed_equity_share",
    "annual_turnover", "trades_per_month", "concentration_p90", "cost_of_panic", "disposition_effect",
    "return_chasing_share", "hype_buy_share", "implied_holding_years", "game_level", "loss_acceptance_level",
    "crash_test_sell_drawdown",
]


class BehavioralFeature(BaseModel):
    name: FeatureName
    value: float | None                              # None = not computable from the data given
    unit: str
    source: EvidenceSource
    n_evidence: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    note: str = ""


class CandidateAssessment(BaseModel):
    menu_index: int
    equity_weight: float
    scenario_loss: float | None                      # positive fraction in config.stress_scenario
    fit: Literal["fit", "caution", "misfit", "no_evidence"]
    reasons: list[str]                               # reason codes for the Advisor, not prose


class BehavioralReport(BaseModel):
    investor_id: str
    as_of: date
    status: Literal["ok", "insufficient_data", "invalid_data"]
    data_source: Literal["real", "synthetic", "none"]

    constraints: list[Constraint]                    # exactly what evaluate_menu returns to Agent A
    features: list[BehavioralFeature]
    evidence: list[Evidence]
    menu_assessment: list[CandidateAssessment]
    advisor_facts: list[str]                         # short factual sentences the Advisor may explain

    stress_scenario: str
    risk_class: RiskClass                            # the questionnaire class, read from Agent A's menu
    capitulation_threshold: float | None
    threshold_source: EvidenceSource | None
    game_level: int | None
    loss_acceptance_level: int | None = None         # from the app's Risk Game score, level 1-5
    crash_test_threshold: float | None = None        # Crash Test: loss from the peak at the first (sizeable) sale
    revealed_equity_share: float | None
    revealed_class: RiskClass | None                 # lowest class whose equity cap covers the revealed share
    implied_risk_aversion: float | None              # diagnostic built on assumptions

    flags: list[str]
    issues: list[str]                                # why data was rejected (status invalid_data)
    input_hash: str
    version: str
    label: str = ("past behavior, a hypothetical game and hypothetical scenarios; comparing a scenario "
                  "loss with a realized drawdown is an approximation; not a forecast")


class ScreeningResult(BaseModel):
    """Goal-based path: which of Agent A's candidates fit this investor (Agent A simulates only these)."""

    approved: list[int]                       # candidate indices, in menu order
    band_lower: float                         # comfort band on the stress loss (positive fractions)
    band_upper: float
    stress_scenario: str
    constraints: list[Constraint]             # B's limits (as evaluate_menu would return them)
    ignored: list[str]                        # labels of limits too weak for Agent A to apply
    relaxed: list[str]                        # soft limits dropped because no candidate met them all
    rejected: dict[int, str]                  # candidate index -> why it was not kept
    counts: dict[str, int]                    # candidates, broke_limit, outside_band, added, thinned, approved
    facts: list[str]                          # plain sentences for the report and the Advisor
    report: BehavioralReport
