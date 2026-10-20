"""Config loading and the small JSON caches. The only file in the briefing that touches the disk
(besides fetch.py's network adapters and the scripts)."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from egoquant.briefing.models import SourceType
from egoquant.settings import cache_dir

_HERE = Path(__file__).parent
CONFIG_PATH = _HERE / "config.yaml"
DATA_DIR = _HERE / "data"
FIXTURE_ITEMS = DATA_DIR / "fixture_items.json"
FIXTURE_SCORES = DATA_DIR / "fixture_scores.json"
FIXTURE_LLM_CACHE = DATA_DIR / "fixture_llm_cache.json"
FIXTURE_FUND_TONES = DATA_DIR / "fixture_fund_tones.json"     # fund-aware labels for the invented sample items


# --------------------------------------------------------------------------
# Config models (validated at load time)
# --------------------------------------------------------------------------
class WindowCfg(BaseModel):
    lookback_days: float = Field(gt=0)
    period_days: float = Field(gt=0)
    max_items: int = Field(gt=0)

    @model_validator(mode="after")
    def _two_periods(self) -> "WindowCfg":
        if self.lookback_days < 2 * self.period_days:
            raise ValueError("lookback_days must cover at least two periods")
        return self


class ToneDisplayCfg(BaseModel):
    strong: float = Field(gt=0, le=1)
    slight: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> "ToneDisplayCfg":
        if not self.slight < self.strong:
            raise ValueError("tone.display.slight must be smaller than tone.display.strong")
        return self


class ToneCfg(BaseModel):
    method: Literal["llm_fund_aware", "finbert"] = "llm_fund_aware"
    llm_model: str = "gpt-4.1-mini"
    llm_max_calls_per_run: int = Field(100, ge=0)
    llm_values: dict[Literal["positive", "negative", "neutral"], float] = Field(
        default_factory=lambda: {"positive": 1.0, "neutral": 0.0, "negative": -1.0})
    display: ToneDisplayCfg = Field(default_factory=lambda: ToneDisplayCfg(strong=0.5, slight=0.15))
    header_note: str = ""
    finbert_header_note: str = ""
    model: str
    source_weights: dict[SourceType, float]
    recency_half_life_days: float = Field(gt=0)
    min_items_for_aggregate: int = Field(ge=1)
    neutral_gating: bool = True


class AlertCfg(BaseModel):
    tone_change_threshold: float = Field(gt=0, le=2)
    min_exposure: float = Field(ge=0, le=1)


PublisherFrom = Literal["feed", "link_domain", "author", "source_element"]
SourceTier = Literal["tier1", "tier2", "unrated", "tier3"]


class SourcesCfg(BaseModel):
    """Source reliability tiers (PROVISIONAL). tier3 is listed but never used for tone or alerts."""

    future_date_tolerance_minutes: float = Field(15, ge=0)
    tier_weights: dict[Literal["tier1", "tier2", "unrated"], float]
    tier_labels: dict[SourceTier, str]
    tier_explanations: dict[SourceTier, str] = Field(default_factory=dict)
    tiers: dict[SourceTier, dict[str, str]]          # tier -> {publisher name: one-line reason}
    tier3_title_patterns: list[str] = Field(default_factory=list)
    publisher_domains: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_tier_each(self) -> "SourcesCfg":
        seen: dict[str, str] = {}
        for tier, names in self.tiers.items():
            for name in names:
                if name in seen:
                    raise ValueError(f"source {name!r} is in both {seen[name]} and {tier}")
                seen[name] = tier
        for p in self.tier3_title_patterns:
            re.compile(p, re.IGNORECASE)
        return self


class CuratedSource(BaseModel):
    name: str
    url: str
    type: SourceType
    headline_only: bool = False              # keep headline, link and date only (drop the feed's summary)
    strip_publisher_suffix: bool = False     # Google News: "Headline - Publisher" -> "Headline", source = Publisher
    publisher_from: PublisherFrom = "feed"   # where the real publisher comes from (see config live_sources)


class TickerFeed(BaseModel):
    """A free per-ticker headline feed; url_template contains {ticker}."""

    name: str
    url_template: str
    type: SourceType
    headline_only: bool = True
    strip_publisher_suffix: bool = False
    publisher_from: PublisherFrom = "feed"

    @model_validator(mode="after")
    def _has_placeholder(self) -> "TickerFeed":
        if "{ticker}" not in self.url_template:
            raise ValueError(f"{self.name}: url_template must contain {{ticker}}")
        return self


def expand_query_feeds(feeds: list[QueryFeed], queries: list[str]) -> list[CuratedSource]:
    """One source per search feed and query (fund names, topic phrases)."""
    from urllib.parse import quote_plus
    return [CuratedSource(name=f.name, url=f.url_template.format(query=quote_plus(q)), type=f.type,
                          headline_only=f.headline_only, strip_publisher_suffix=f.strip_publisher_suffix,
                          publisher_from=f.publisher_from)
            for f in feeds for q in dict.fromkeys(q.strip() for q in queries if q.strip())]


def expand_ticker_feeds(feeds: list[TickerFeed], tickers: list[str]) -> list[CuratedSource]:
    """One source per feed and ticker, for an EXPLICIT ticker list (never implicitly the whole catalog)."""
    from urllib.parse import quote
    return [CuratedSource(name=f.name, url=f.url_template.format(ticker=quote(t)), type=f.type,
                          headline_only=f.headline_only, strip_publisher_suffix=f.strip_publisher_suffix,
                          publisher_from=f.publisher_from)
            for f in feeds for t in dict.fromkeys(tickers)]


class DiversityCfg(BaseModel):
    max_share_one_type: float = Field(gt=0, le=1)
    one_sided_tone: float = Field(gt=0, le=1)
    min_items: int = Field(ge=1)
    curated_sources: list[CuratedSource]


class CadenceRule(BaseModel):
    min_horizon_years: float = Field(ge=0)
    frequency: str


class CadenceCfg(BaseModel):
    rules: list[CadenceRule] = Field(min_length=1)
    alerts_between_briefings: bool = True


class PeriodPreset(BaseModel):
    period_days: float = Field(gt=0)
    lookback_days: float = Field(gt=0)
    recency_half_life_days: float = Field(gt=0)
    this: str
    last: str

    @model_validator(mode="after")
    def _two_periods(self) -> "PeriodPreset":
        if self.lookback_days < 2 * self.period_days:
            raise ValueError("a period preset's lookback must cover this period and the previous one")
        return self


class LLMCfg(BaseModel):
    enabled: bool
    model_env: str
    model_default: str
    temperature: float = Field(ge=0, le=2)
    max_output_tokens: int = Field(gt=0, le=1000)
    timeout_seconds: float = Field(gt=0)
    max_llm_calls_per_run: int = Field(ge=0)
    prompt_version: str
    summary_max_chars: int = Field(gt=0)
    summary_max_sentences: int = Field(gt=0)

    def model_name(self) -> str:
        return os.environ.get(self.model_env, "").strip() or self.model_default


class QueryFeed(BaseModel):
    """A free search feed; url_template contains {query} (URL-encoded on expansion)."""

    name: str
    url_template: str
    type: SourceType
    headline_only: bool = True
    strip_publisher_suffix: bool = False
    publisher_from: PublisherFrom = "feed"


class LiveSourcesCfg(BaseModel):
    rss: list[CuratedSource]
    rss_per_ticker: list[TickerFeed] = Field(default_factory=list)
    query_feeds: list[QueryFeed] = Field(default_factory=list)
    name_queries: bool = False
    topic_queries: dict[str, list[str]] = Field(default_factory=dict)
    user_agent: str = "EgoQuant/0.1"
    request_pause_seconds: float = Field(0.0, ge=0, le=10)
    max_live_tickers: int = Field(15, ge=1)
    subreddits: list[str]
    reddit_posts_per_subreddit: int = Field(gt=0, le=100)
    edgar_enabled: bool
    edgar_filings_per_ticker: int = Field(gt=0)
    tiingo_news_enabled: bool = False

    @model_validator(mode="after")
    def _no_paid_news(self) -> "LiveSourcesCfg":
        if self.tiingo_news_enabled:
            raise ValueError("Tiingo news needs a paid plan and is disabled by the cost rule")
        return self


class EvalCfg(BaseModel):
    sample_size: int = Field(gt=0)
    random_seed: int
    max_rows_per_ticker: int = Field(20, ge=1)
    llm_neutral_band: float = Field(ge=0, lt=1)


class RegionSignalsCfg(BaseModel):
    us: list[str] = Field(default_factory=list)
    foreign: list[str] = Field(default_factory=list)
    generic_terms: list[str] = Field(default_factory=list)
    crypto: list[str] = Field(default_factory=list)
    us_region_areas: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compile(self) -> "RegionSignalsCfg":
        for p in [*self.us, *self.foreign, *self.crypto]:
            re.compile(p)
        return self


class ExposureCfg(BaseModel):
    factor: float = Field(0.6, ge=0, le=1)
    cap_per_theme: dict[str, int] = Field(default_factory=lambda: {"week": 15, "month": 40})
    group_above_holdings: int = Field(6, ge=1)
    small_position: float = Field(0.02, ge=0, le=1)


class ThemeCfg(BaseModel):
    label: str
    region: Literal["us"] | None = None
    not_with_crypto: bool = False          # stock-market themes: crypto headlines never count
    match: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compile(self) -> "ThemeCfg":
        for p in self.match:
            re.compile(p, re.IGNORECASE)
        return self


class ThemeRule(BaseModel):
    when: dict[str, Any]
    themes: list[str]
    stop: bool = False


class ConfidenceCfg(BaseModel):
    k: float = Field(1.5, ge=0)
    medium: float = Field(1.5, gt=0)
    high: float = Field(4.0, gt=0)


class NewsWorkerCfg(BaseModel):
    interval_minutes: float = Field(15, gt=0)
    max_tickers: int = Field(120, ge=1)
    per_ticker_feeds: list[str] = Field(default_factory=list)
    topic_queries: bool = True
    fetch_seconds: float = Field(90, gt=0)
    parallel_fetch: int = Field(8, ge=1)
    parallel_labels: int = Field(10, ge=1)
    labels_per_run: int = Field(1500, ge=0)
    labels_per_day: int = Field(3000, ge=0)
    retries: int = Field(2, ge=0)


class BriefingJobCfg(BaseModel):
    fetch_seconds: float = Field(25, gt=0)
    parallel_labels: int = Field(10, ge=1)
    retries: int = Field(2, ge=0)
    max_seconds: float = Field(120, gt=0)
    max_pairs: int = Field(300, ge=0)
    max_jobs: int = Field(50, ge=1)
    ttl_minutes: float = Field(30, gt=0)
    per_window: int = Field(20, ge=1)


class BriefingConfig(BaseModel):
    version: str
    briefing_periods: dict[str, PeriodPreset] = Field(default_factory=dict)
    default_briefing_period: str = "week"
    window: WindowCfg
    dedupe: dict[str, float]
    truncation: dict[str, int]
    tone: ToneCfg
    alerts: AlertCfg
    suitability: dict[str, str]
    diversity: DiversityCfg
    cadence: CadenceCfg
    llm: LLMCfg
    forbidden_patterns: list[str] = Field(min_length=1)
    ticker_stopwords: list[str]
    capital_only_tickers: list[str] = Field(default_factory=list)
    exchange_tag_prefixes: list[str] = Field(default_factory=list)
    non_us_exchange_prefixes: list[str] = Field(default_factory=list)
    ambiguous_stock_names: list[str] = Field(default_factory=list)
    non_news_patterns: list[str] = Field(default_factory=list)
    instrument_keywords: dict[str, list[str]]
    live_sources: LiveSourcesCfg
    sources: SourcesCfg
    news_worker: NewsWorkerCfg = Field(default_factory=NewsWorkerCfg)
    region_signals: RegionSignalsCfg = Field(default_factory=RegionSignalsCfg)
    exposure: ExposureCfg = Field(default_factory=ExposureCfg)
    exposure_themes: dict[str, ThemeCfg] = Field(default_factory=dict)
    theme_rules: list[ThemeRule] = Field(default_factory=list)
    theme_overrides: dict[str, list[str]] = Field(default_factory=dict)
    confidence: ConfidenceCfg = Field(default_factory=ConfidenceCfg)
    briefing_job: BriefingJobCfg = Field(default_factory=BriefingJobCfg)
    eval: EvalCfg

    @model_validator(mode="after")
    def _themes_known(self) -> "BriefingConfig":
        known = set(self.exposure_themes)
        used = {t for r in self.theme_rules for t in r.themes} | {t for ts in self.theme_overrides.values() for t in ts}
        if used - known:
            raise ValueError(f"unknown exposure themes: {sorted(used - known)}")
        return self

    @model_validator(mode="after")
    def _patterns_compile(self) -> "BriefingConfig":
        for p in [*self.forbidden_patterns, *self.non_news_patterns]:
            re.compile(p, re.IGNORECASE)
        return self

    @property
    def cadence_rules(self) -> list[CadenceRule]:
        return sorted(self.cadence.rules, key=lambda r: -r.min_horizon_years)


def load_config(path: Path | str = CONFIG_PATH) -> BriefingConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return BriefingConfig.model_validate(raw)


# --------------------------------------------------------------------------
# JSON caches (dict of key -> JSON value). Live caches live outside the repository.
# --------------------------------------------------------------------------
def briefing_cache_dir() -> Path:
    return cache_dir() / "briefing"


def read_json_cache(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:          # JSONDecodeError and UnicodeDecodeError are ValueErrors
        # A damaged cache must never break a briefing: start empty (it is rebuilt from the next fetch).
        logging.getLogger(__name__).warning("Cache file %s unreadable (%s); starting empty", path.name,
                                            type(exc).__name__)
        return {}


def write_json_cache(path: Path, data: dict) -> None:
    """Atomic: a temp file of its own per write (threads and processes never share one), then one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=False)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def with_period(config: BriefingConfig, period: str | None) -> tuple[BriefingConfig, PeriodPreset]:
    """A copy of the config with the period preset applied (window and recency half-life), plus the preset.
    None = config.default_briefing_period. Min items per aggregate and every other setting are unchanged."""
    name = period or config.default_briefing_period
    if name not in config.briefing_periods:
        raise ValueError(f"unknown briefing_period {name!r}; choose one of {sorted(config.briefing_periods)}")
    preset = config.briefing_periods[name]
    cfg = config.model_copy(deep=True)
    cfg.window.period_days = preset.period_days
    cfg.window.lookback_days = preset.lookback_days
    cfg.tone.recency_half_life_days = preset.recency_half_life_days
    return cfg, preset
