"""Data contracts for the market briefing. Module-specific, so they live here, not in egoquant/models.py.

The briefing is shown ALONGSIDE the portfolio. Nothing here is an input to Agent A, Agent B or the
Risk Engine, and the only action an alert can carry is offer_risk_profile_recheck.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal["outlet", "analyst", "forum", "filing"]
DataSource = Literal["live", "fixture"]
SummaryKind = Literal["llm", "template", "withheld"]
DateStatus = Literal["published", "updated_only", "unknown"]
SourceTier = Literal["tier1", "tier2", "unrated", "tier3"]
ToneMethod = Literal["llm_fund_aware", "finbert"]
ToneLabel = Literal["positive", "negative", "neutral"]
AlertAction = Literal["offer_risk_profile_recheck"]          # the ONLY action that exists

LABELS = [
    "AI-generated summaries",
    "News tone is a measurement of what sources say, not a forecast",
    "Does not change your portfolio",
]
AI_SUMMARIES_LABEL = LABELS[0]
NO_AI_SUMMARIES_LABEL = "Summaries repeat each source's headline (no AI summaries)"
DEMO_BANNER = "DEMO: sample news items, not real news."


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------
class NewsItem(BaseModel):
    """One headline from one source, normalized. Only title, the feed's own summary and the link are kept."""

    model_config = ConfigDict(frozen=True)

    id: str                                      # stable hash of source name + link
    title: str
    summary_text: str = ""                       # the feed's summary/description, truncated; never a full article
    url: str
    published_at: datetime                       # the feed's own date; for date_status "unknown" the fetch time
    source_name: str                             # the real publisher ("24/7 Wall St."), not the feed
    source_type: SourceType
    fetched_at: datetime
    data_source: DataSource
    feed: str = ""                               # where we got it ("Yahoo Finance", "Nasdaq", "Google News"); "" = same
    date_status: DateStatus = "published"        # published | updated_only (only an <updated> date) | unknown

    @property
    def publisher(self) -> str:
        return self.source_name


class ToneScore(BaseModel):
    """FinBERT output for one item. tone = P(positive) - P(negative), in [-1, 1]. Not a forecast."""

    positive: float = Field(ge=0, le=1)
    negative: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    tone: float = Field(ge=-1, le=1)
    model: str


class LLMSummary(BaseModel):
    """Structured output the LLM must return. Validated again after the call."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)   # the config limit is checked separately
    llm_tone: float = Field(ge=-1, le=1)                   # stored for evaluation only, never shown as the tone
    mentions: list[str] = Field(default_factory=list, max_length=20)   # logging only, never used for exposure


class FundTone(BaseModel):
    """Tone of one headline for holders of one fund. value: LLM label mapped to +1 / 0 / -1, or FinBERT's gated
    P(pos) - P(neg) (the same for every fund of the headline). Not a forecast."""

    ticker: str
    label: ToneLabel
    value: float = Field(ge=-1, le=1)
    method: ToneMethod


class BriefingItem(BaseModel):
    item: NewsItem
    matched_tickers: list[str]
    matched_areas: list[str]
    publisher: str = ""                          # = item.source_name (the real publisher); feed in item.feed
    source_tier: SourceTier = "unrated"          # tier1 established | tier2 commentary | unrated | tier3 (not for tone)
    tier_reason: str = ""
    tone_by_fund: list[FundTone] = Field(default_factory=list)   # same as fund_tones (the name the UI uses)
    # "About what it holds": held funds this headline concerns through an exposure theme -> theme id / label
    # (matched_tickers are the funds it is ABOUT). Exposure items weigh exposure.factor in the fund's score.
    exposure_of: dict[str, str] = Field(default_factory=dict)
    exposure_labels: dict[str, str] = Field(default_factory=dict)
    exposure: float | None = None                # share of the portfolio in matched tickers; None = no portfolio given
    exposure_text: str = ""
    tone: ToneScore | None = None                # FinBERT (fallback and record); None = model missing / uncached
    fund_tones: list[FundTone] = Field(default_factory=list)   # per matched fund; empty = tone unavailable
    tone_method: Literal["llm_fund_aware", "finbert", "mixed", "unavailable"] = "unavailable"
    summary: str
    summary_kind: SummaryKind
    llm_tone: float | None = None                # evaluation only
    audit: list[str] = Field(default_factory=list)   # why a fallback happened, filter hits, cache hits


# --------------------------------------------------------------------------
# Aggregates, alerts, notes
# --------------------------------------------------------------------------
class Aggregate(BaseModel):
    subject: str                                 # ticker or interest area
    kind: Literal["instrument", "area", "group"]
    n_items: int
    tone: float | None                           # None = not enough coverage or tone unavailable
    n_items_previous: int
    tone_previous: float | None
    change: float | None
    exposure: float | None = None
    text: str
    item_ids: list[str] = Field(default_factory=list)   # the scored headlines behind tone and tone_previous
    # this period's usable headlines by their rating for these funds (+1 / 0 / -1, or not rated yet)
    n_positive: int = 0
    n_neutral: int = 0
    n_negative: int = 0
    n_unrated: int = 0
    confidence: Literal["high", "medium", "low"] | None = None            # this period
    confidence_previous: Literal["high", "medium", "low"] | None = None
    evidence: float = 0.0                        # sum of weights behind the score (tier x recency x relation)
    evidence_previous: float = 0.0
    compare_note: str | None = None              # why there is no change ("too little news last week to compare")
    n_exposure: int = 0                          # this period's headlines about what the fund holds (not the fund)
    themes: list[str] = Field(default_factory=list)          # exposure themes (instrument cards)
    theme_labels: list[str] = Field(default_factory=list)
    # --- for the UI (cards); all optional, so older readers keep working ---
    name: str = ""
    held: bool = False
    weight: float | None = None                  # Agent A's final weight (instrument cards)
    r_level: int | None = None
    areas: list[str] = Field(default_factory=list)
    coverage_status: CoverageStatus | None = None
    instruments: list[AreaInstrument] = Field(default_factory=list)   # area cards
    parent_area: str | None = None               # nested under this chosen area (e.g. us_stocks under stocks)
    headline_ids: list[str] = Field(default_factory=list)   # headlines SHOWN on this card this period, newest first
    n_shown_under_holdings: int = 0              # this card's headlines shown under a holding instead
    shown_under: dict[str, int] = Field(default_factory=dict)   # ... or under another card (subject -> count)


class CoverageStatus(BaseModel):
    """One of: a tone; not enough coverage (n assessed headlines, needs `needed`); not yet assessed (headlines
    waiting for a tone, none assessed). Never "not yet assessed" when any headline is assessed."""

    # tone: a (damped) score with a confidence label; quiet: no news about the fund or what it holds this period;
    # unrated: headlines found but no rating available (no AI rating and no FinBERT). The old states are kept for
    # older readers; this backend no longer produces them.
    state: Literal["tone", "quiet", "unrated", "not_enough_coverage", "not_yet_assessed"]
    n: int = 0                                   # assessed headlines this period
    needed: int = 3
    n_waiting: int = 0                           # headlines this period without a tone yet


class AreaInstrument(BaseModel):
    ticker: str
    name: str
    status: Literal["held", "not_held", "above_risk_level"]
    r_level: int | None = None


class BriefingAlert(BaseModel):
    subject: str
    kind: Literal["instrument", "area"]
    change: float
    exposure: float
    message: str                                 # summary + the re-check offer
    tone: float | None = None                    # this period's level
    tone_previous: float | None = None           # last period's level
    n_items: int = 0
    summary: str = ""                            # the sentence without the offer (the report's "What changed")
    # The re-check offer is made for NEGATIVE moves only (change <= -threshold); a rise is reported without it.
    action: AlertAction | None = "offer_risk_profile_recheck"


class SuitabilityNote(BaseModel):
    area: str
    max_r_level: int
    tickers_above: list[str]
    text: str


class DiversitySuggestion(BaseModel):
    area: str
    reason: str
    name: str
    url: str
    source_type: SourceType


class BriefingMetadata(BaseModel):
    n_fetched: int = 0
    n_non_news: int = 0                          # quote/chart/holdings pages and truncated titles, dropped
    n_after_dedupe: int = 0
    n_outside_window: int = 0
    n_unmatched: int = 0
    n_matched: int = 0
    n_filtered: int = 0                          # LLM summaries replaced because of the advice filter
    n_withheld: int = 0                          # items whose headline itself contained advice wording
    n_llm_calls: int = 0
    n_llm_cache_hits: int = 0
    n_tone_unavailable: int = 0
    tone_method: str = ""                        # configured method: llm_fund_aware | finbert
    n_fund_tones_llm: int = 0                    # (headline, fund) pairs labeled by the fund-aware LLM
    n_fund_tones_finbert: int = 0                # pairs that fell back to FinBERT
    n_fund_tones_unavailable: int = 0            # pairs with neither (no cache, no model)
    n_tone_llm_calls: int = 0                    # new fund-aware calls this run (cached pairs cost nothing)
    tone_fallback_reasons: list[str] = Field(default_factory=list)
    fallback_tone_model: str = ""                # FinBERT model name (the fallback; scored n_fund_tones_finbert pairs)
    final_job: str | None = None                 # set by the briefing job: this is the FINISHED version (every pair rated
                                                 # or given its plain reason); the quick in-request version leaves it None
    tone_llm_model: str = ""                     # fund-aware tone model (scored n_fund_tones_llm pairs)
    llm_model: str = ""
    prompt_version: str = ""
    sources_skipped: list[str] = Field(default_factory=list)
    tickers_left_out_by_cap: list[str] = Field(default_factory=list)   # relevant, but beyond max_live_tickers
    cadence: str = ""
    config_version: str = ""


class HoldingGroup(BaseModel):
    """Holdings grouped by their main exposure theme (large portfolios): "US Treasuries & Fed policy · BIL + SHY"."""

    theme: str
    label: str
    tickers: list[str]
    weight: float
    aggregate: "Aggregate"


class LargestMove(BaseModel):
    subject: str
    kind: Literal["instrument", "area"]
    change: float


class TierLegend(BaseModel):
    tier: SourceTier
    label: str
    explanation: str
    weight: float | None                         # None = not used for tone


class Briefing(BaseModel):
    as_of: datetime
    investor_id: str
    data_source: Literal["live", "fixture", "mixed"]
    period_start: datetime
    period_end: datetime
    items: list[BriefingItem]
    instrument_aggregates: list[Aggregate]
    area_aggregates: list[Aggregate]
    alerts: list[BriefingAlert]
    suitability_notes: list[SuitabilityNote]
    diversity_suggestions: list[DiversitySuggestion]
    metadata: BriefingMetadata
    labels: list[str] = Field(default_factory=lambda: list(LABELS))
    tone_note: str = ""                          # header line: how tone is assessed (and how well, from the evaluation)
    briefing_period: str = "week"                # the window summarised: week | month (cadence is separate)
    banners: list[str] = Field(default_factory=list)
    # --- for the UI ---
    period_words: dict[str, str] = Field(default_factory=dict)   # {"this": "this week", "last": "last week"}
    alert_threshold: float | None = None         # alerts start at +/- this change
    holdings_order: list[str] = Field(default_factory=list)      # held tickers, largest weight first
    chosen_areas: list[str] = Field(default_factory=list)        # the investor's interest areas, in order
    tier_counts: dict[str, int] = Field(default_factory=dict)    # this period's headlines per source tier
    method_counts: dict[str, int] = Field(default_factory=dict)  # (headline, fund) pairs per tone method
    low_reliability_items: list[str] = Field(default_factory=list)   # tier3 item ids this period (listed, not used)
    single_tier_notes: list[str] = Field(default_factory=list)
    largest_move: LargestMove | None = None      # for the empty "What changed" state
    tier_legend: list[TierLegend] = Field(default_factory=list)
    n_date_unknown: int = 0                      # headlines without a feed date (listed, not in tone windows)
    month: "Briefing | None" = None              # the same fetch summarised by month (web /portfolio only)
    compare_notes: list[str] = Field(default_factory=list)   # holdings that cannot be compared, and why
    holding_groups: list[HoldingGroup] = Field(default_factory=list)   # set when there are many holdings
    small_positions: list[str] = Field(default_factory=list)  # holdings below exposure.small_position

    @model_validator(mode="after")
    def _demo_banner(self) -> "Briefing":
        if self.data_source in ("fixture", "mixed") and DEMO_BANNER not in self.banners:
            self.banners = [DEMO_BANNER, *self.banners]
        return self

    @field_validator("alerts")
    @classmethod
    def _only_recheck(cls, alerts: list[BriefingAlert]) -> list[BriefingAlert]:
        for a in alerts:
            if a.action not in (None, "offer_risk_profile_recheck"):
                raise ValueError("the only allowed alert action is offer_risk_profile_recheck (or none)")
            if a.action is not None and a.change > 0:
                raise ValueError("a rise in news tone never carries the re-check offer")
        return alerts


Briefing.model_rebuild()
