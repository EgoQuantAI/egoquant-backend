"""Agent C: Market Intelligence, job 1 (market data for Agent A).

Flow:

  Directory (directory.py): every US-listed stock and ETF by NAME, sorted into interest-area clusters
           (Nasdaq names, sectors and market caps; Tiingo's ticker list; Tiingo IEX volume), most traded first
  Preference form (after the C-level) --InvestorInterests--> Agent C (validated fields, never raw chat)
  Agent C: interest areas + trading platforms -> the investor's clusters  (platform, R-level and product
                                                                     filters; see UniverseSelection.queues)
  Agent C: fetch_universe walks those clusters, most traded first: an instrument Tiingo cannot price is
           replaced by the next one in the SAME cluster, until the data budget or the caps are reached
           (Tiingo's free tier is ~50 requests an hour; cached prices are free). Crypto investors get the
           whole Binance market priced; Agent A receives the most traded coins up to DEFAULT_MAX_CRYPTO.
  Agent C picks a REAL data feed per asset: Binance for crypto spot, Tiingo for US-listed funds and stocks
  Agent A --get_market_snapshot(asset_ids, as_of, lookback_days)--> Agent C
  Agent C: fetch -> point-in-time cut -> clean -> align -> MarketSnapshot --> Agent A
  Agent C also keeps a SnapshotReport (what was dropped and why, drawdown alerts) for the Advisor.

Agent C implements the MarketDataProvider protocol from egoquant/models.py exactly, so
QuantAgent(MarketIntelligenceAgent(...), ...) works with no change to Agent A.

Rules Agent C follows (from the system context document):
  * Agent A fetches no data. Agent C supplies prices and metadata; Agent A still validates them.
  * Nothing dated after `as_of` is ever returned (no look-ahead).
  * Data is labeled: "synthetic" or "historical". Historical is not a forecast.
  * Agent C never changes Agent A's numbers and never says buy or sell. Alerts are framed as
    "what changed", and a sharp drop is only a reason to offer a risk-profile re-check.
  * Every universe decision (included, excluded, briefing only) carries a written reason.

Instruments the investor is interested in but may not hold (product risk above their R-level, a
product type blocked by their knowledge score, or an asset class Agent A cannot model) go on a
WATCHLIST: Agent C still prices them (get_watchlist_snapshot) for briefings and drawdown alerts, but
they are never sent to Agent A.

Job 2 (news briefings with a tone score) is Agent D: egoquant/briefing/ (see its README). Drawdown alerts stay here.
"""

from __future__ import annotations

import math
import time as _time
from collections import Counter, deque
from datetime import date, timedelta
from typing import get_args

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from egoquant.agents.market_intel.catalog import (
    DEFAULT_CATALOG,
    InstrumentStyle,
    Instrument,
    InterestArea,
    ProductType,
    Sector,
    catalog_by_ticker,
    normalize_interest_area,
    region_of,
    resolve_ticker,
)
from egoquant.agents.market_intel.directory import Directory, load_directory, merge_catalog
from egoquant.agents.market_intel.platforms import PLATFORMS, normalize_platform
from egoquant import settings
from egoquant.agents.market_intel.sources import (
    BinanceMarket,
    PriceSource,
    PriceSourceError,
    RateLimitError,
    RoutedPriceSource,
    SyntheticPriceSource,
    build_live_source,
    tiingo_requests_in_last,
)
from egoquant.agents.market_intel.snapshot import (
    SnapshotPriceSource,
    agent_c_snapshot_path,
    agent_c_snapshot_paths,
    load_snapshot,
)
from egoquant.models import AssetClass, AssetInfo, MarketSnapshot

# Asset classes Agent A can optimize. Read from the shared model so that extending AssetClass
# automatically lets those instruments into the universe.
AGENT_A_ASSET_CLASSES: frozenset[str] = frozenset(get_args(AssetClass))


# --------------------------------------------------------------------------
# Input from the Advisor chatbot
# --------------------------------------------------------------------------
class InvestorInterests(BaseModel):
    """What the investor wants to invest in, as validated fields from the chatbot.

    interest_areas accepts canonical names ("real_estate") or common phrasings ("Real estate",
    "Gold & metals", "bitcoin"); anything unrecognized is rejected with the list of allowed values,
    so the chatbot can ask the user again instead of Agent C guessing.

    max_r_level and blocked_product_types come from the risk profile module (numeric limits only;
    Agent C never sees the questionnaire score).
    """

    investor_id: str
    interest_areas: list[InterestArea] = Field(min_length=1)
    max_r_level: int | None = Field(None, ge=1, le=5)          # highest product risk level allowed
    blocked_product_types: list[ProductType] = Field(default_factory=list)  # knowledge restriction
    held_assets: list[str] = Field(default_factory=list)       # tickers in the current portfolio
    # Where the investor trades (chatbot asks). Decides what can be bought and where prices come from.
    # Empty = not asked yet: no platform filter, and a note says so.
    trading_platforms: list[str] = Field(default_factory=list)
    # Instrument preferences (egoquant/preferences). interest_areas is in PRIORITY order: when the
    # universe budget is tight, earlier areas are served first.
    instrument_style: InstrumentStyle = "funds"                  # funds / individual stocks / a mix
    avoid_sectors: list[Sector] = Field(default_factory=list)    # industries the investor does not want
    wants_individual_bonds: bool = False                         # noted: only bond funds can be priced

    @model_validator(mode="after")
    def _no_wanted_and_avoided_sector(self) -> "InvestorInterests":
        both = sorted(set(self.avoid_sectors) & set(self.interest_areas))
        if both:
            raise ValueError(f"{both} cannot be both an interest and an industry to avoid")
        return self

    @field_validator("trading_platforms", mode="before")
    @classmethod
    def _normalize_platforms(cls, value: object) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("trading_platforms must be a list of strings")
        keys: list[str] = []
        for raw in value:
            key = normalize_platform(str(raw))
            if key not in keys:
                keys.append(key)
        return keys

    @field_validator("interest_areas", mode="before")
    @classmethod
    def _normalize_areas(cls, value: object) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("interest_areas must be a list of strings")
        areas: list[str] = []
        for raw in value:
            area = normalize_interest_area(str(raw))
            if area not in areas:
                areas.append(area)
        return areas

    @field_validator("held_assets", mode="before")
    @classmethod
    def _upper_tickers(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return [str(t).strip().upper() for t in value]
        return value


# --------------------------------------------------------------------------
# Outputs (Agent C's own; the shared MarketSnapshot is what Agent A receives)
# --------------------------------------------------------------------------
class SelectedInstrument(BaseModel):
    ticker: str
    name: str
    asset_class: str
    product_type: str
    r_level: int                                       # PLACEHOLDER R-level; the Risk Engine re-checks it
    reasons: list[str]                                 # "core", "interest:gold", "held"


class BriefingOnlyArea(BaseModel):
    area: str
    tickers: list[str]
    reason: str


class ExcludedInstrument(BaseModel):
    ticker: str
    reason: str


class UniverseSelection(BaseModel):
    """Which assets Agent A may use for this investor, and why. Also read by the Advisor and Risk Engine."""

    investor_id: str
    interest_areas: list[str]
    trading_platforms: list[str] = Field(default_factory=list)
    instruments: list[SelectedInstrument]               # sent to Agent A
    # The order Agent C fetches prices in: core funds and holdings, then the interests round-robin
    fetch_order: list[str] = Field(default_factory=list)
    # Interest area (cluster) -> every allowed candidate, most relevant and most traded first. fetch_universe
    # walks these; an instrument that cannot be priced is replaced by the next one of the same cluster.
    queues: dict[str, list[str]] = Field(default_factory=dict)
    # Funds tracking the same exposure as a selected one, tried in order if it cannot be priced
    alternates: dict[str, list[str]] = Field(default_factory=dict)
    # Allowed but left out to keep the universe within budget (lower priority than what was kept)
    deprioritized: list["ExcludedInstrument"] = Field(default_factory=list)
    # Selected but not priced by fetch_universe (data budget, rate limit, no data or too little history)
    not_fetched: list["ExcludedInstrument"] = Field(default_factory=list)
    # Interest-area instruments the investor may not hold: priced for briefings, NOT sent to Agent A
    watchlist: list[SelectedInstrument] = Field(default_factory=list)
    briefing_only: list[BriefingOnlyArea] = Field(default_factory=list)
    excluded: list[ExcludedInstrument] = Field(default_factory=list)
    suitability_flags: list[str] = Field(default_factory=list)   # raise these in chat at selection time
    assets_to_exclude: list[str] = Field(default_factory=list)   # held but not allowed: put these in
                                                                 # InvestorContext.excluded_assets
    notes: list[str] = Field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return [i.ticker for i in self.instruments]

    @property
    def watchlist_tickers(self) -> list[str]:
        return [i.ticker for i in self.watchlist]

    @property
    def r_levels(self) -> dict[str, int]:
        return {i.ticker: i.r_level for i in self.instruments}


class DroppedAsset(BaseModel):
    ticker: str
    reason: str


class DrawdownAlert(BaseModel):
    ticker: str
    window_days: int
    drawdown_from_peak: float                          # negative fraction, e.g. -0.12
    message: str


class SnapshotReport(BaseModel):
    """What happened while building one MarketSnapshot. Agent A's DataQualityReport is separate."""

    as_of: date
    source: str
    data_source: str
    requested: list[str]
    delivered: list[str]
    dropped: list[DroppedAsset]
    n_rows: int
    first_date: date | None
    last_date: date | None
    price_sources: dict[str, str] = Field(default_factory=dict)  # ticker -> where its prices came from
    filled_values: dict[str, int]                      # forward-filled prices per asset (holidays, gaps)
    risk_free_rate_annual: float
    risk_free_method: str
    alerts: list[DrawdownAlert]
    fallback_reason: str | None = None   # why the live feed was replaced (by the snapshot or SYNTHETIC prices)
    prices_as_of: date | None = None     # set when prices came from the committed snapshot: the day they run to
    price_note: str | None = None        # e.g. "real prices as of 2026-10-16, cached"
    snapshot_first: bool = False         # the snapshot was CHOSEN (EGOQUANT_PRICE_MODE=snapshot), not a fallback
    notes: list[str] = Field(default_factory=list)   # e.g. crypto left out because its feed failed
    label: str = "historical or synthetic data as labeled; not a forecast"


class FetchReport(BaseModel):
    """What fetch_universe priced for this investor, and what it cost against the data budget."""

    as_of: date
    budget: dict[str, int]                             # most NEW requests allowed, per market
    new_requests: dict[str, int]                       # NEW requests made, per market
    fetched: list[str]                                 # priced, in fetch order
    from_cache: list[str]                              # of those, served from disk (no request)
    replaced: dict[str, str] = Field(default_factory=dict)   # selected -> same-exposure fund used instead
    not_fetched: list[ExcludedInstrument] = Field(default_factory=list)   # the most relevant ones, with why
    n_not_fetched: int = 0                             # all candidates looked at but not priced
    crypto_market_priced: int = 0                      # Binance pairs priced (the whole market for crypto investors)
    rate_limited: list[str] = Field(default_factory=list)    # markets whose source refused more requests


class CryptoMarketEntry(BaseModel):
    ticker: str
    base: str
    volume_rank: int                                   # 1 = most traded on Binance spot (24h, in USDT)
    quote_volume_24h: float | None
    last_close: float
    change_1d: float | None
    change_30d: float | None
    change_90d: float | None
    drawdown_from_peak: float                          # vs the highest close in the window (<= 0)


class CryptoMarketReport(BaseModel):
    """The whole Binance spot market (USDT pairs) for briefings. Never sent to Agent A."""

    as_of: date
    n_pairs_listed: int
    entries: list[CryptoMarketEntry]                   # priced pairs, by volume rank
    dropped: list[DroppedAsset]
    alerts: list[DrawdownAlert]
    label: str = "historical data from Binance; not a forecast and not a buy or sell signal"


# This many network failures in a row (no request answered in between) mean the host is unreachable, not that two
# tickers had a bad moment: the whole run switches source instead of trying hundreds more candidates one by one.
UNREACHABLE_IN_A_ROW = 2


def _unreachable(exc: Exception) -> bool:
    return "could not be reached" in str(exc)


def _feed_level(exc: Exception) -> bool:
    """A failure of the whole feed (switch the run's source) rather than of one request (skip that ticker)."""
    text = str(exc)
    return any(k in text for k in ("refused the API key", "refused the request from this location",
                                   "TIINGO_API_KEY is not set"))


class MarketDataUnavailable(RuntimeError):
    """A core asset (cash, bond or broad equity fund) could not be delivered, or nothing could."""


# --------------------------------------------------------------------------
# Universe selection: interest areas -> instruments
# --------------------------------------------------------------------------
# PROVISIONAL caps on what Agent A receives. Agent A's optimizer takes ~20 s for ~130 instruments and grows
# quickly beyond that, and a portfolio build must finish inside the app's 2-minute request limit.
DEFAULT_MAX_INSTRUMENTS = 100       # most instruments sent to Agent A
DEFAULT_MAX_SINGLE_STOCKS = 50      # at most this many single stocks in one universe
DEFAULT_MAX_CRYPTO = 30             # at most this many crypto-assets (the class is capped at 5% of a portfolio)
DEFAULT_MAX_PER_AREA = 25           # at most this many instruments from one interest area (cluster)
# Portfolio method v2 (docs/PORTFOLIO_V2.md): Agent C sends EVERY eligible product, none of the four limits above; this is
# only a safety ceiling on the candidates it will try to price (the catalog has 292 today). PROVISIONAL.
MAX_UNIVERSE = 400
TIINGO_HOURLY_REQUESTS = 50          # Tiingo's free tier: requests an hour per account
ELIGIBLE_AREA = "__eligible__"     # v2: queue of the eligible products that match none of the investor's interests
MAX_WATCHLIST_PER_AREA = 8          # interests the investor may not hold: the most traded few, for briefings
MAX_LISTED = 50                     # longest list of left-out instruments kept with reasons (the rest is counted)
LATE_START_DAYS = 10                # PROVISIONAL: prices must start within this many days of the analysis window
STABLECOIN_DAILY_VOL = 0.002        # PROVISIONAL: a "coin" moving less than 0.2% a day is a stablecoin, not crypto
# PROVISIONAL: most NEW price requests per portfolio build, per market (see fetch_universe). Tiingo's
# free tier allows 50 requests an hour (1,000 a day, 500 distinct tickers a month); Binance's public
# market data allows far more, so crypto investors get the whole Binance market. Prices already cached
# on disk cost nothing, so repeated builds can use more of the clusters over time.
DEFAULT_FETCH_BUDGET: dict[str, int] = {"us_listed": 40, "crypto_spot": 1000}


def select_universe(interests: InvestorInterests, catalog: list[Instrument] | None = None,
                    max_instruments: int = DEFAULT_MAX_INSTRUMENTS,
                    max_single_stocks: int = DEFAULT_MAX_SINGLE_STOCKS,
                    max_crypto: int = DEFAULT_MAX_CRYPTO,
                    max_per_area: int = DEFAULT_MAX_PER_AREA,
                    all_eligible: bool = False) -> UniverseSelection:
    """Deterministic: same interests + same catalog -> same universe, in catalog order.

    all_eligible (portfolio method v2): every product that passes the filters below is eligible, whether or not it
    matches an interest area (interests only decide the fetch priority and which assets Agent A must keep), and
    nothing is trimmed: no `max_instruments`, per-area, single-stock or crypto limit, and funds of one exposure group
    (VOO, IVV, SPY) stay apart. Only the safety ceiling MAX_UNIVERSE applies. With all_eligible False (method v1) nothing below changes.

    Filters, in order: trading platform, instrument style (funds vs single stocks), industries to
    avoid, asset classes Agent A cannot model, R-level and blocked product types. Funds of one
    exposure group (e.g. VOO, IVV, SPY) are reduced to the first one; the rest become its fallbacks.
    Then the budget: core funds and current holdings are always kept; the rest is filled round-robin
    over the interest areas in the investor's priority order (within an area: instruments matching
    more of the investor's interests first, then the most common, i.e. catalog order), until
    max_instruments is reached (and at most max_single_stocks single stocks and max_crypto
    crypto-assets, so one asset class cannot crowd out the rest). That order is also the fetch order.
    `queues` keeps every candidate of each chosen area in that order; fetch_universe walks them and
    replaces what cannot be priced with the next candidate of the same area.
    """
    catalog = catalog or DEFAULT_CATALOG
    by_ticker = catalog_by_ticker(catalog)           # also rejects a catalog with duplicate tickers
    avoid = set(interests.avoid_sectors)
    style = interests.instrument_style
    max_r = interests.max_r_level
    blocked = set(interests.blocked_product_types)
    platforms = [PLATFORMS[k] for k in interests.trading_platforms]
    tradable_markets = {m for p in platforms for m in p.markets}
    platform_names = ", ".join(p.name for p in platforms)
    stock_sectors = frozenset(i.sector for i in catalog if i.product_type == "stock" and i.sector)

    notes: list[str] = []
    held: set[str] = set()
    for raw in interests.held_assets:
        ticker = resolve_ticker(raw, catalog)
        if ticker is None:
            notes.append(f"held asset {raw} is not in Agent C's catalog, so it cannot be priced; "
                         "leave it out of InvestorContext.current_holdings or add it to the catalog")
        else:
            held.add(ticker)
    if not platforms:
        notes.append("trading platform not given: no platform filter applied (ask the user where they trade)")

    reasons: dict[str, list[str]] = {}
    for inst in catalog:
        if inst.core:
            reasons.setdefault(inst.ticker, []).append("core")
        for area in interests.interest_areas:
            if area in inst.areas:
                reasons.setdefault(inst.ticker, []).append(f"interest:{area}")
        if inst.ticker in held:
            reasons.setdefault(inst.ticker, []).append("held")
        if all_eligible and inst.ticker not in reasons:
            reasons[inst.ticker] = ["eligible"]

    selected: list[SelectedInstrument] = []
    watchlist: list[SelectedInstrument] = []
    watched: Counter = Counter()                       # watchlist entries per interest area
    excluded: list[ExcludedInstrument] = []
    assets_to_exclude: list[str] = []
    core_off_platform = False
    for inst in catalog:  # catalog order keeps the output deterministic
        if inst.ticker not in reasons:
            continue
        why = reasons[inst.ticker]
        off_platform = bool(platforms) and inst.market not in tradable_markets
        if off_platform and not inst.core and "held" not in why:
            excluded.append(ExcludedInstrument(
                ticker=inst.ticker, reason=f"not tradable on the investor's platform(s) ({platform_names})"))
            continue
        core_off_platform |= off_platform and inst.core
        if "held" not in why and not inst.core:
            if not _fits_style(inst, style, stock_sectors):   # not what they asked for; not an exclusion
                continue
            if inst.sector in avoid:
                excluded.append(ExcludedInstrument(
                    ticker=inst.ticker, reason=f"you asked to avoid the {inst.sector.replace('_', ' ')} industry"))
                continue
        entry = SelectedInstrument(ticker=inst.ticker, name=inst.name, asset_class=inst.asset_class,
                                   product_type=inst.product_type, r_level=inst.r_level, reasons=why)
        if inst.asset_class not in AGENT_A_ASSET_CLASSES:
            watchlist.append(entry)  # priced for briefings and alerts, never sent to Agent A
            if "held" in why:
                notes.append(f"{inst.ticker} is held but Agent A cannot model {inst.asset_class} yet: leave it out "
                             "of InvestorContext.current_holdings; Agent A's weights then cover the rest of the "
                             "portfolio, and Agent C tracks it on the watchlist")
            continue
        problem = None
        if max_r is not None and inst.r_level > max_r:
            problem = f"product risk R{inst.r_level} is above the investor's maximum R{max_r}"
        elif inst.product_type in blocked:
            problem = f"product type {inst.product_type} is blocked for this investor (knowledge restriction)"
        if problem and "held" not in why:
            excluded.append(ExcludedInstrument(ticker=inst.ticker, reason=problem))
            asked = [r for r in why if r.startswith("interest:")]
            if asked and watched[asked[0]] < MAX_WATCHLIST_PER_AREA:
                watchlist.append(entry)  # the investor asked about it: keep the most traded few in briefings
                watched[asked[0]] += 1
            continue
        if problem:  # already held: keep it priceable so Agent A can compute the trade, but no new money
            assets_to_exclude.append(inst.ticker)
            notes.append(f"{inst.ticker} is held but not allowed ({problem}); it is in the universe only "
                         "so Agent A can plan selling it; put it in InvestorContext.excluded_assets")
        selected.append(entry)

    if all_eligible:       # method v2 keeps VOO, IVV and SPY apart: Agent A's screening picks the cheapest of them
        alternates = {}
    else:
        selected, alternates = _one_per_exposure(selected, by_ticker)
    queues = _area_queues(selected, interests.interest_areas)
    if all_eligible:
        selected, deprioritized, fetch_order, rest = _all_eligible_order(selected, interests.interest_areas, queues)
        queues[ELIGIBLE_AREA] = rest
    else:
        selected, deprioritized, fetch_order = _apply_budget(selected, interests.interest_areas, by_ticker,
                                                             max_instruments,
                                                             {"stock": max_single_stocks, "crypto": max_crypto},
                                                             max_per_area)
    in_queues = set(fetch_order) | {t for q in queues.values() for t in q}
    alternates = {t: alts for t, alts in alternates.items() if t in in_queues}
    watch = {i.ticker for i in watchlist}
    if len(excluded) > MAX_LISTED:                     # keep the reasons the report needs (watchlist) + a sample
        kept = [e for e in excluded if e.ticker in watch] + [e for e in excluded if e.ticker not in watch][:MAX_LISTED]
        notes.append(f"{len(excluded)} instruments of the chosen areas are not allowed or not tradable for this "
                     "investor; the most traded ones are listed")
        excluded = kept
    if deprioritized and all_eligible:
        notes.append(f"{len(deprioritized)} eligible instrument(s) were left out by the safety ceiling of "
                     f"{MAX_UNIVERSE} instruments; the investor's interests, core funds and holdings came first")
    elif deprioritized:
        notes.append(f"{len(deprioritized)} allowed instrument(s) were left out to keep the universe at "
                     f"{max_instruments} instruments; the most common ones of each chosen area were kept first")
    if avoid:
        notes.append("broad funds (for example VT) still hold some companies from the industries you avoid; "
                     "only the industry funds and single stocks are left out")
    if interests.wants_individual_bonds:
        notes.append("individual bonds have no licensed free price source here, so bond funds are used instead")

    if core_off_platform:
        notes.append(f"the core funds (cash, bonds, broad equity) are US-listed ETFs, which cannot be bought on "
                     f"{platform_names}; Agent A's portfolio is then a model allocation that needs a brokerage "
                     "account with US market access")

    briefing_only: list[BriefingOnlyArea] = []
    flags: list[str] = []
    for area in interests.interest_areas:
        members = [i for i in catalog if area in i.areas and _fits_style(i, style, stock_sectors)
                   and (not platforms or i.market in tradable_markets)]
        if platforms and not members:
            flags.append(f"interest '{area}': nothing in Agent C's catalog for it can be traded on {platform_names}")
            continue
        outside = [i for i in members if i.asset_class not in AGENT_A_ASSET_CLASSES]
        if outside and len(outside) == len(members):
            classes = sorted({i.asset_class for i in outside})
            briefing_only.append(BriefingOnlyArea(
                area=area, tickers=[i.ticker for i in outside],
                reason=f"asset class {', '.join(classes)} is outside Agent A's universe "
                       f"({', '.join(sorted(AGENT_A_ASSET_CLASSES))}); this area feeds briefings only"))
        if max_r is not None and members and all(i.r_level > max_r for i in members):
            top = min(i.r_level for i in members)
            flags.append(f"interest '{area}' involves products rated R{top} or higher, above the R{max_r} "
                         "your risk profile allows; it can be covered in briefings but not added to your portfolio")
        elif members and all(i.product_type in blocked for i in members):
            flags.append(f"interest '{area}' involves product types blocked by your knowledge assessment "
                         f"({', '.join(sorted({i.product_type for i in members}))})")

    if not selected:
        raise MarketDataUnavailable("no instrument is allowed for this investor; check max_r_level and blocks")
    classes_present = {i.asset_class for i in selected}
    if not classes_present & {"cash", "bond"}:
        notes.append("no cash or bond asset is allowed; Agent A may find low-risk portfolios infeasible")
    n_equity = sum(i.asset_class == "equity" for i in selected)
    if n_equity <= 2:
        notes.append(f"only {n_equity} equity fund(s) in the universe; with Agent A's per-asset cap "
                     f"(max_asset_weight, 40% by default) total equity cannot exceed {n_equity} x that cap")

    return UniverseSelection(
        investor_id=interests.investor_id, interest_areas=list(interests.interest_areas),
        trading_platforms=list(interests.trading_platforms), instruments=selected, fetch_order=fetch_order,
        queues=queues, alternates=alternates, deprioritized=deprioritized[:MAX_LISTED], watchlist=watchlist, briefing_only=briefing_only, excluded=excluded,
        suitability_flags=flags, assets_to_exclude=assets_to_exclude, notes=notes,
    )


def _fits_style(inst: Instrument, style: str, stock_sectors: frozenset[str]) -> bool:
    """Single stocks only for investors who want them ("individual" or "mix"); for "individual",
    an industry fund gives way to that industry's single stocks when the catalog has some
    (stock_sectors: the industries that have single stocks in the catalog)."""
    if inst.product_type == "stock":
        return style != "funds"
    if style == "individual" and inst.sector is not None:
        return inst.sector not in stock_sectors
    return True


def _group(inst: Instrument) -> str | None:
    """Budget group with its own limit: single stocks, crypto-assets, or none."""
    return "stock" if inst.product_type == "stock" else "crypto" if inst.asset_class == "crypto" else None


def _one_per_exposure(selected: list[SelectedInstrument], by_ticker: dict[str, Instrument]
                      ) -> tuple[list[SelectedInstrument], dict[str, list[str]]]:
    """Keep the first fund of each exposure group; the others become its fallbacks, in catalog order.

    A core fund or a holding represents its group (both must be priced anyway), so e.g. AGG stands
    for the aggregate-bond group and BND becomes AGG's fallback.
    """
    fixed = {i.ticker for i in selected if "core" in i.reasons or "held" in i.reasons}
    representative: dict[str, str] = {}
    for i in selected:
        group = by_ticker[i.ticker].exposure_group
        if group and i.ticker in fixed:
            representative.setdefault(group, i.ticker)
    kept: list[SelectedInstrument] = []
    alternates: dict[str, list[str]] = {}
    for i in selected:
        group = by_ticker[i.ticker].exposure_group
        if group is None or i.ticker in fixed or representative.setdefault(group, i.ticker) == i.ticker:
            kept.append(i)
        else:
            alternates.setdefault(representative[group], []).append(i.ticker)
    return kept, alternates


def _area_queues(selected: list[SelectedInstrument], areas: list[str]) -> dict[str, list[str]]:
    """Interest area -> its candidates (not core funds or holdings), most relevant then most traded first.

    Relevance: an instrument that matches several of the investor's interests (MCHI for "china" and
    "emerging_markets") comes before one matching a single interest. sorted() is stable, so ties keep
    catalog order (the directory's ranking: most traded first).
    """
    fixed = {i.ticker for i in selected if "core" in i.reasons or "held" in i.reasons}
    matches = {i.ticker: sum(r.startswith("interest:") for r in i.reasons) for i in selected}
    return {a: sorted((i.ticker for i in selected if f"interest:{a}" in i.reasons and i.ticker not in fixed),
                      key=lambda t: -matches[t]) for a in areas}


def _all_eligible_order(selected: list[SelectedInstrument], areas: list[str], queues: dict[str, list[str]]
                        ) -> tuple[list[SelectedInstrument], list[ExcludedInstrument], list[str], list[str]]:
    """Portfolio method v2: nothing is trimmed except by the safety ceiling MAX_UNIVERSE.

    Priority (= the fetch order): core funds, holdings, the investor's interest areas in their order (each area's most
    relevant first), then every other eligible product in catalog order. Returns the kept instruments (catalog order),
    the ones the ceiling left out, the priority order, and the "other eligible" queue Agent C walks last."""
    order = [i.ticker for i in selected if "core" in i.reasons] + \
            [i.ticker for i in selected if "held" in i.reasons and "core" not in i.reasons]
    seen = set(order)
    for area in areas:
        for ticker in queues.get(area, []):
            if ticker not in seen:
                seen.add(ticker)
                order.append(ticker)
    rest = [i.ticker for i in selected if i.ticker not in seen]
    order += rest
    keep = set(order[:MAX_UNIVERSE])
    kept = [i for i in selected if i.ticker in keep]
    dropped = [ExcludedInstrument(ticker=i.ticker, reason=f"left out by the safety ceiling of {MAX_UNIVERSE} instruments")
               for i in selected if i.ticker not in keep]
    return kept, dropped, order[:MAX_UNIVERSE], [t for t in rest if t in keep]


def _apply_budget(selected: list[SelectedInstrument], areas: list[str], by_ticker: dict[str, Instrument],
                  max_instruments: int, group_limits: dict[str, int], max_per_area: int = DEFAULT_MAX_PER_AREA
                  ) -> tuple[list[SelectedInstrument], list[ExcludedInstrument], list[str]]:
    """Keep core funds and holdings, then fill the budget round-robin over the interest areas.

    Returns the kept instruments (catalog order), the ones left out, and the priority (fetch) order.
    """
    order = [i.ticker for i in selected if "core" in i.reasons] + \
            [i.ticker for i in selected if "held" in i.reasons and "core" not in i.reasons]
    keep = set(order)
    used = {g: sum(_group(by_ticker[t]) == g for t in keep) for g in group_limits}
    by_area = _area_queues(selected, areas)
    queues = [list(by_area[a]) for a in areas]
    served = [0] * len(areas)
    while len(keep) < max_instruments and any(q for n, q in enumerate(queues) if served[n] < max_per_area):
        for n, queue in enumerate(queues):
            while queue and queue[0] in keep:
                queue.pop(0)
            if not queue or len(keep) >= max_instruments or served[n] >= max_per_area:
                continue
            ticker = queue.pop(0)
            group = _group(by_ticker[ticker])
            if group in group_limits:
                if used[group] >= group_limits[group]:
                    continue
                used[group] += 1
            keep.add(ticker)
            order.append(ticker)
            served[n] += 1
    kept = [i for i in selected if i.ticker in keep]
    dropped = [ExcludedInstrument(ticker=i.ticker, reason=f"left out to keep the universe at {max_instruments} "
                                                          "instruments; more common choices came first")
               for i in selected if i.ticker not in keep]
    return kept, dropped, order


# --------------------------------------------------------------------------
# Agent C
# --------------------------------------------------------------------------
class MarketIntelligenceAgent:
    """Agent C. One instance per investor (it holds that investor's approved universe)."""

    def __init__(
        self,
        interests: InvestorInterests,
        source: PriceSource | None = None,
        catalog: list[Instrument] | None = None,
        max_instruments: int = DEFAULT_MAX_INSTRUMENTS,   # universe budget, see select_universe
        directory: Directory | None | str = "auto",       # "auto": the investment directory when prices are live
        binance_market: BinanceMarket | None = None,  # None = live Binance listing when prices are live
        risk_free_rate_annual: float | None = None,   # None = estimate from the cash fund's trailing return
        fallback_risk_free_rate: float = 0.03,
        min_history_rows: int = 253,                  # Agent A's default window_days + 1
        max_fill_days: int = 3,                       # longest gap that may be forward-filled
        stale_days: int = 7,                          # older last price than this -> asset dropped
        drawdown_window_days: int = 21,               # OPEN: team still to set the re-check rule
        drawdown_alert_threshold: float = 0.10,
        # Demo-proofing: if a live feed fails (no key, key refused, rate limit, blocked region), the WHOLE run
        # switches to the committed snapshot of REAL prices, then (opt-in) to SYNTHETIC prices, labeled as such.
        fallback_to_synthetic: bool | None = None,    # None = settings.data_fallback() == "synthetic"
        use_snapshot: bool | None = None,             # None = settings.price_snapshot_enabled()
        snapshot_path: str | None = None,             # None = the committed Yahoo snapshot (agent_c_snapshot_path)
        max_snapshot_age_days: int = 30,              # PROVISIONAL: older snapshot -> not used
        price_mode: str | None = None,                # None = settings.price_mode(): "snapshot" = no live request at all
        live_budget_seconds: float | None = None,     # None = settings.live_budget_seconds()
        slow_request_seconds: float | None = None,    # None = settings.slow_request_seconds()
        clock=_time.monotonic,                        # tests pass a fake clock
        portfolio_method: str | None = None,          # None = settings.portfolio_method(); "v2": every eligible product
    ):
        self.interests = interests
        # Portfolio method v2: every eligible product, none of the v1 trimming (see select_universe). v1 is unchanged.
        self.all_eligible = (settings.portfolio_method() if portfolio_method is None else portfolio_method) == "v2"
        if self.all_eligible and max_instruments == DEFAULT_MAX_INSTRUMENTS:
            max_instruments = MAX_UNIVERSE
        self.max_instruments = max_instruments
        self.clock = clock
        self.live_budget_seconds = settings.live_budget_seconds() if live_budget_seconds is None else live_budget_seconds
        self.slow_request_seconds = settings.slow_request_seconds() if slow_request_seconds is None else slow_request_seconds
        # When a live feed fails: the committed snapshot of REAL prices first, then (opt-in) SYNTHETIC.
        self.fallback_to_synthetic = (settings.data_fallback() == "synthetic"
                                      if fallback_to_synthetic is None else fallback_to_synthetic)
        self.use_snapshot = settings.price_snapshot_enabled() if use_snapshot is None else use_snapshot
        self.snapshot_path = snapshot_path or str(agent_c_snapshot_path())
        self._snapshot_paths = [snapshot_path] if snapshot_path else [str(p) for p in agent_c_snapshot_paths()]
        self.max_snapshot_age_days = max_snapshot_age_days
        self.fallback_reason: str | None = None
        self.notes: list[str] = []
        # Snapshot price mode: read the committed snapshot of REAL prices directly; no live request of any kind.
        self.snapshot_first = False
        if source is None and (settings.price_mode() if price_mode is None else price_mode) == "snapshot":
            chosen = self._snapshot_for_snapshot_mode()
            if chosen is not None:
                source, self.snapshot_first = chosen, True
        # Every listed stock and ETF by name, in interest-area clusters (directory.py). Live runs load it
        # (rebuilt weekly); offline sources keep the curated catalog unless a Directory is passed in.
        if directory == "auto":
            directory = load_directory() if source is None and catalog is None else None
        self.directory: Directory | None = directory if isinstance(directory, Directory) else None
        self.catalog = catalog or merge_catalog(self.directory, DEFAULT_CATALOG)
        # Crypto-eligible investors get the WHOLE Binance spot market, ranked by trading volume, so the
        # budget picks the most traded coins and the market report can cover every coin. Offline
        # sources (synthetic, CSV) keep the fixed catalog unless a BinanceMarket is passed in.
        self.crypto_volumes: dict[str, float] = {}
        if self._crypto_eligible(interests) and (binance_market is not None or source is None):
            try:
                self.catalog = self._with_binance_market(self.catalog, binance_market or BinanceMarket())
            except PriceSourceError as exc:   # e.g. HTTP 451 from a US server: keep the fixed crypto pairs
                self.notes.append(f"the Binance coin list could not be loaded ({exc}); only the catalog's "
                                  "fixed crypto pairs were considered")
        self.instruments = catalog_by_ticker(self.catalog)
        self.selection = select_universe(interests, self.catalog, max_instruments, all_eligible=self.all_eligible)
        if source is None:  # REAL data by default: one feed per market this investor needs
            markets = {self.instruments[t].market
                       for t in self.selection.tickers + self.selection.watchlist_tickers}
            try:
                source = build_live_source(markets, self.catalog)
            except PriceSourceError as exc:
                source = self._replacement(None, exc)
        self.source: PriceSource = source
        self.risk_free_rate_annual = risk_free_rate_annual
        self.fallback_risk_free_rate = fallback_risk_free_rate
        self.min_history_rows = min_history_rows
        self.max_fill_days = max_fill_days
        self.stale_days = stale_days
        self.drawdown_window_days = drawdown_window_days
        self.drawdown_alert_threshold = drawdown_alert_threshold
        self.last_report: SnapshotReport | None = None
        self.last_fetch: FetchReport | None = None
        self.last_watchlist_prices: pd.DataFrame | None = None
        self._cache: tuple[frozenset[str], date, date, pd.DataFrame] | None = None
        # get_market_snapshot answers for the same (assets, as_of, lookback, source, price memory) are shared inside a run
        self._snapshot_memo: dict[tuple, tuple[MarketSnapshot, SnapshotReport]] = {}

    # ---- step 1: fetch this investor's universe within the data budget ----
    def fetch_universe(self, as_of: date, lookback_days: int, budget: dict[str, int] | None = None,
                       max_instruments: int | None = None, max_per_area: int = DEFAULT_MAX_PER_AREA,
                       max_single_stocks: int = DEFAULT_MAX_SINGLE_STOCKS,
                       max_crypto: int = DEFAULT_MAX_CRYPTO, history_days: int | None = None,
                       max_seconds: float | None = None) -> FetchReport:
        """Walk the investor's clusters, most traded first, and price as many instruments as allowed.

        max_seconds: after this long, no NEW request is made for an optional candidate (core funds and holdings
        are always priced); cached prices are still used. The web app sets it so that no build waits minutes for
        a slow or rate-limited price feed. None = no time limit (CLI, warm-up).

        history_days: also fetch this much history (trading days) in the same requests, e.g. ~10 years for
        Agent A's goal simulation. Candidates are still judged on the `lookback_days` window; the longer
        range costs no extra request (one request per ticker returns any date range) and later requests
        for it (get_price_history) are served from memory instead of fetching everything again.

        1. Core funds and holdings are always priced (no budget limit).
        2. Crypto investors: the whole Binance market is priced in parallel (Binance's limits allow it).
        3. Round-robin over the chosen interest areas in the investor's priority order: each area's next
           candidate is priced; one that cannot be (not on Tiingo, too little history, stale) is replaced
           by the next candidate of the SAME area, a fund first by one tracking the same thing. Stops at
           `max_per_area` per area, `max_single_stocks`, `max_crypto` and `max_instruments` in total.
        Prices on disk cost nothing; each NEW request counts against `budget` for its market
        (DEFAULT_FETCH_BUDGET) and against Tiingo's monthly limit of distinct tickers. Once the budget is
        used, cached candidates can still be added. Afterwards the selection holds what was priced (core
        funds and holdings stay even when they failed: get_market_snapshot then reports them), and
        get_market_snapshot for the same window is served from memory: Agent A makes no requests.
        """
        while True:
            try:
                return self._fetch_universe(as_of, lookback_days, budget, max_instruments, max_per_area,
                                            max_single_stocks, max_crypto, history_days, max_seconds)
            except PriceSourceError as exc:
                # The WHOLE run switches to the next source (snapshot, then opt-in synthetic); raises when
                # none is left. Nothing fetched from the failed feed is reused, so prices are never mixed.
                self.source = self._replacement(self.source, exc)
                self._cache = None

    def _fetch_universe(self, as_of: date, lookback_days: int, budget: dict[str, int] | None,
                        max_instruments: int | None, max_per_area: int, max_single_stocks: int,
                        max_crypto: int, history_days: int | None = None,
                        max_seconds: float | None = None) -> FetchReport:
        budget = dict(DEFAULT_FETCH_BUDGET if budget is None else budget)
        deadline = None if max_seconds is None else _time.monotonic() + max_seconds
        max_instruments = max_instruments or self.max_instruments
        if self.all_eligible:                          # method v2: no trimming, and never beyond Tiingo's hourly quota
            max_instruments = max_per_area = max_single_stocks = max_crypto = MAX_UNIVERSE
            budget = self._within_tiingo_quota(budget)
        start = self.window_start(as_of, lookback_days)            # what candidates are judged on
        fetch_start = self.window_start(as_of, max(lookback_days, history_days or 0))   # what is fetched
        sel = self.selection
        held = {i.ticker for i in sel.instruments if "held" in i.reasons}
        spent: dict[str, int] = {}
        limited: set[str] = set()                      # markets whose source hit its rate limit
        series: dict[str, pd.Series] = {}
        from_cache: list[str] = []
        replaced: dict[str, str] = {}
        failed: dict[str, str] = {}                    # ticker -> why it was not priced (priority order)
        prefetched: set[str] = set()
        unreachable = [0]                              # network failures in a row
        live = [0.0, 0, 0]                             # seconds waiting for live requests, requests, slow requests

        def timed(tickers: list[str], first: date, count: bool) -> pd.DataFrame:
            """One request; when it really goes to a live feed, its duration counts against the build's live budget."""
            started = self.clock()
            try:
                return self.source.fetch_adjusted_close(tickers, first, as_of)
            finally:
                if count:
                    took = self.clock() - started
                    live[0] += took
                    live[1] += 1
                    live[2] += took > self.slow_request_seconds

        def too_slow() -> None:
            """The whole build switches to the snapshot (one source per run) when the live feed is slow: more than the
            live budget in total, or two requests over the slow limit. Only when the snapshot (or the opt-in synthetic
            fallback) can take over, otherwise a slow answer is still better than none."""
            if (live[0] > self.live_budget_seconds or live[2] >= 2) and (
                    self._snapshot_usable(as_of) or self.fallback_to_synthetic):
                raise PriceSourceError(f"the price feed answered too slowly ({live[0]:.0f} s for {live[1]} request(s), "
                                       f"{live[2]} of them slow): this build uses the saved snapshot instead")

        def price(candidate: str, fixed: bool) -> str | None:
            """Fetch one instrument; None when it was priced, else the reason it was not."""
            market = self.instruments[candidate].market
            paid = self._needs_request(candidate, fetch_start, as_of) and candidate not in prefetched
            # Already on disk for the analysis window (just not the longer goal history): usable for free;
            # the goal simulation then completes its earlier years (goal.complete_history).
            short_cached = fetch_start < start and not self._needs_request(candidate, start, as_of)
            use_start = fetch_start
            if paid and not fixed:
                blocked = None
                if market in limited:
                    blocked = f"the {market} price source's rate limit was reached; try again later"
                elif spent.get(market, 0) >= budget.get(market, math.inf):
                    blocked = (f"not fetched: the data budget ({budget[market]} new {market} requests per build) "
                               "was used by more relevant choices")
                elif not self._allows_new(candidate):
                    blocked = "not fetched: Tiingo's monthly limit of distinct tickers is nearly used"
                elif deadline is not None and _time.monotonic() > deadline:
                    blocked = ("not fetched in time: its prices are not cached yet and this build's time limit for new "
                               "price requests was reached; it is used once the price cache has it")
                if blocked:
                    if not short_cached:
                        return blocked
                    use_start, paid = start, False
            try:
                frame = timed([candidate], use_start, paid)
            except RateLimitError:
                limited.add(market)
                if not short_cached:
                    if fixed and self._snapshot_usable(as_of):
                        # A core fund or holding cannot be priced: get_market_snapshot would switch the WHOLE run
                        # to the snapshot anyway, after this walk had dropped every uncached candidate. Switch
                        # now, so the clusters are walked on the snapshot's prices (one source for the run).
                        raise
                    return f"the {market} price source's rate limit was reached; try again later"
                frame = self.source.fetch_adjusted_close([candidate], start, as_of)   # from disk, no request
                paid = False
            except PriceSourceError as exc:
                if _feed_level(exc):
                    raise                    # key refused / region blocked: the whole run switches source
                if _unreachable(exc):
                    unreachable[0] += 1
                    if unreachable[0] >= UNREACHABLE_IN_A_ROW:
                        raise                # the host cannot be reached at all: switch source now
                too_slow()                   # a failure that took long also counts (a timeout is a slow request)
                return f"price request failed ({exc}); the next candidate is used"
            unreachable[0] = 0
            too_slow()
            spent[market] = spent.get(market, 0) + int(paid)
            # Judge the candidate on the analysis window only (an old trading halt years ago is no reason
            # to drop it); the longer history fetched for the goal simulation is kept as it is.
            window = frame.loc[pd.Timestamp(start) - pd.Timedelta(days=LATE_START_DAYS + 5):]
            clean, dropped, _ = self._clean(window, [candidate], as_of, trim_late=False)
            if candidate not in clean.columns:
                return dropped[0].reason if dropped else "no data from the source"
            prices = clean[candidate]
            if not fixed:
                # The same rule the snapshot applies later (trim_late), checked now so that the next
                # candidate of the cluster can take its place instead of a silent drop.
                if prices.index[0] > pd.Timestamp(start) + pd.Timedelta(days=LATE_START_DAYS):
                    return (f"prices start {prices.index[0].date()}, later than the analysis window; keeping it "
                            "would shorten every estimate")
                if self.instruments[candidate].asset_class == "crypto" and \
                        float(prices.pct_change().std()) < STABLECOIN_DAILY_VOL:
                    return "behaves like a stablecoin (its price barely moves): cash-like, not a crypto-asset"
            series[candidate] = frame[candidate]
            if not paid and candidate not in prefetched:
                from_cache.append(candidate)
            return None

        def attempt(ticker: str, fixed: bool) -> str | None:
            """Price `ticker`, else a fund tracking the same thing; returns what was priced."""
            for candidate in [ticker, *sel.alternates.get(ticker, [])]:
                if candidate in series:
                    return candidate
                if candidate in failed:
                    continue
                why = price(candidate, fixed)
                if why is None:
                    if candidate != ticker:
                        replaced[ticker] = candidate
                    return candidate
                failed[candidate] = why
            return None

        # 1. core funds and holdings
        delivered: list[str] = []
        fixed = [t for t in sel.fetch_order if self.instruments[t].core or t in held]
        for ticker in fixed:
            got = attempt(ticker, True)
            if got and got not in delivered:
                delivered.append(got)

        # 2. the whole Binance market, in parallel, for investors who chose crypto
        crypto = [t for a in sel.interest_areas for t in sel.queues.get(a, [])
                  if self.instruments[t].market == "crypto_spot"]
        if self.all_eligible:                          # the other eligible coins too (Binance serves them in parallel)
            crypto += [t for t in sel.queues.get(ELIGIBLE_AREA, []) if self.instruments[t].market == "crypto_spot"]
        prefetched |= self._prefetch(list(dict.fromkeys(crypto)), fetch_start, as_of, budget, spent, limited)

        # 3. round-robin over the clusters
        areas = [a for a in sel.interest_areas if sel.queues.get(a)]
        if self.all_eligible and sel.queues.get(ELIGIBLE_AREA):
            areas.append(ELIGIBLE_AREA)                # method v2: the products that match no interest, walked last
        queues = {a: deque(sel.queues[a]) for a in areas}
        served: Counter = Counter()
        caps = {"stock": max_single_stocks, "crypto": max_crypto}
        used = Counter(_group(self.instruments[t]) for t in delivered)
        while len(delivered) < max_instruments:
            progress = False
            for area in areas:
                queue = queues[area]
                while queue and served[area] < max_per_area and len(delivered) < max_instruments:
                    ticker = queue.popleft()
                    if ticker in delivered or replaced.get(ticker) in delivered:
                        served[area] += 1                  # already serves another chosen area too
                        progress = True
                        break
                    group = _group(self.instruments[ticker])
                    if group in caps and used[group] >= caps[group]:
                        continue
                    got = attempt(ticker, False)
                    if got:
                        delivered.append(got)
                        used[_group(self.instruments[got])] += 1
                        served[area] += 1
                        progress = True
                        break                              # next area's turn
            if not progress:
                break

        # The selection now holds what was priced, in priority order.
        entries = {i.ticker: i for i in sel.instruments}
        instruments: list[SelectedInstrument] = []
        for ticker in delivered:
            original = next((t for t, alt in replaced.items() if alt == ticker), None)
            base = entries.get(original or ticker) or self._selected(self.instruments[original or ticker], held)
            inst = self.instruments[ticker]
            instruments.append(SelectedInstrument(
                ticker=ticker, name=inst.name, asset_class=inst.asset_class, product_type=inst.product_type,
                r_level=inst.r_level, reasons=[*base.reasons, f"replaces:{original}"] if original else base.reasons))
        for ticker in fixed:                               # a core fund or holding that failed stays, reported later
            if ticker not in delivered and ticker not in replaced:
                instruments.append(entries[ticker])
        missing = [(t, why) for t, why in failed.items() if t not in delivered and t not in replaced
                   and not (self.instruments[t].core or t in held)]
        not_fetched = [ExcludedInstrument(ticker=t, reason=why) for t, why in missing[:MAX_LISTED]]

        notes = list(sel.notes)
        n_new = sum(spent.values())
        notes.append(f"Agent C priced {len(delivered)} instrument(s) for this investor: {len(from_cache)} from its "
                     f"cache and {n_new} with new requests (budget: "
                     + ", ".join(f"{n} {m}" for m, n in budget.items()) + ").")
        for t, alt in replaced.items():
            notes.append(f"{t} could not be priced ({failed.get(t) or 'no usable data'}); {alt}, which tracks "
                         "the same market, is used instead")
        if missing:
            notes.append(f"{len(missing)} allowed instrument(s) were not priced (data budget, not on Tiingo or too "
                         "little history); the next ones of the same area were used instead where possible")
        # Crypto pairs whose prices are now on hand (fetched this run or already cached), not only new ones.
        crypto_priced = sum(1 for t in dict.fromkeys(crypto)
                            if t in series or t in prefetched or not self._needs_request(t, fetch_start, as_of))
        self.selection = sel.model_copy(update={
            "instruments": instruments, "not_fetched": not_fetched, "notes": notes, "fetch_order": delivered})
        frame = pd.concat(series, axis=1, sort=True) if series else pd.DataFrame(index=pd.DatetimeIndex([]))
        self._cache = (frozenset(series), fetch_start, as_of, frame)
        self.last_fetch = FetchReport(
            as_of=as_of, budget=budget, new_requests=spent, fetched=delivered, from_cache=from_cache,
            replaced=replaced, not_fetched=not_fetched, n_not_fetched=len(missing),
            crypto_market_priced=crypto_priced, rate_limited=sorted(limited))
        return self.last_fetch

    def _prefetch(self, tickers: list[str], start: date, end: date, budget: dict[str, int],
                  spent: dict[str, int], limited: set[str], workers: int = 8) -> set[str]:
        """Fetch uncached crypto pairs in parallel so their prices land in the disk cache; returns them."""
        from concurrent.futures import ThreadPoolExecutor

        todo = [t for t in tickers if self._needs_request(t, start, end)]
        room = budget.get("crypto_spot", math.inf) - spent.get("crypto_spot", 0)
        todo = todo[: max(0, int(min(room, len(todo))))]
        if not todo:
            return set()

        def one(ticker: str) -> str | None:
            try:
                self.source.fetch_adjusted_close([ticker], start, end)
                return ticker
            except RateLimitError:
                limited.add("crypto_spot")
                return None
            except PriceSourceError as exc:
                if _feed_level(exc):
                    raise
                return None                  # one coin's glitch: skip it, keep the run on live prices

        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = {t for t in pool.map(one, todo) if t}
        spent["crypto_spot"] = spent.get("crypto_spot", 0) + len(done)
        return done

    def _selected(self, inst: Instrument, held: set[str]) -> SelectedInstrument:
        """A selection entry for an instrument fetched from deeper in a cluster."""
        reasons = (["core"] if inst.core else []) + \
            [f"interest:{a}" for a in self.interests.interest_areas if a in inst.areas] + \
            (["held"] if inst.ticker in held else [])
        return SelectedInstrument(ticker=inst.ticker, name=inst.name, asset_class=inst.asset_class,
                                  product_type=inst.product_type, r_level=inst.r_level, reasons=reasons)

    # ---- the MarketDataProvider method Agent A calls ----
    SNAPSHOT_MEMO_SIZE = 4

    def _memo_key(self, requested: list[str], as_of: date, lookback_days: int) -> tuple:
        """What a snapshot depends on: the assets asked for (None and the full list are the same ask), the date, the
        window, and the data version (a source switch or a new fetch moves it)."""
        return (tuple(requested), as_of, lookback_days, self._data_version)

    # A counter that moves whenever the price source or the price memory is replaced: part of the memo key (an object id
    # could be reused by a new object after the old one is freed; a counter cannot).
    _data_version = 0
    _source_value = None
    _cache_value = None

    @property
    def source(self):
        return self._source_value

    @source.setter
    def source(self, value) -> None:
        self._source_value = value
        self._data_version += 1

    @property
    def _cache(self):
        return self._cache_value

    @_cache.setter
    def _cache(self, value) -> None:
        self._cache_value = value
        self._data_version += 1

    @staticmethod
    def _copy_snapshot(snap: MarketSnapshot) -> MarketSnapshot:
        """A caller may change what it gets back (prices are a DataFrame): hand out copies, keep the stored one pristine."""
        return MarketSnapshot(as_of=snap.as_of, prices=snap.prices.copy(), assets=[a.model_copy() for a in snap.assets],
                              risk_free_rate_annual=snap.risk_free_rate_annual, data_source=snap.data_source)

    def get_market_snapshot(self, asset_ids: list[str] | None, as_of: date, lookback_days: int) -> MarketSnapshot:
        universe = self.selection.tickers
        if asset_ids is None:
            requested = universe
        else:
            unknown = sorted(set(asset_ids) - set(universe))
            if unknown:
                raise ValueError(f"assets outside this investor's approved universe: {unknown}; "
                                 f"approved: {universe}")
            requested = [t for t in universe if t in asset_ids]

        remembered = self._snapshot_memo.get(self._memo_key(requested, as_of, lookback_days))
        if remembered is not None:                   # the same ask again (Agent A, the pipeline, the routes): no second clean-up
            self.last_report = remembered[1]
            return self._copy_snapshot(remembered[0])

        start = self.window_start(as_of, lookback_days)
        raw = self._fetch(requested, start, as_of)   # switches to the snapshot / synthetic itself on failure
        prices, dropped, filled = self._clean(raw, requested, as_of)

        core_lost = [d for d in dropped if self.instruments[d.ticker].core]
        if core_lost or prices.empty:
            detail = "; ".join(f"{d.ticker}: {d.reason}" for d in (core_lost or dropped)) or "no data"
            raise MarketDataUnavailable(f"cannot build a snapshot for {as_of}: {detail}")

        prices = prices.tail(lookback_days + 1)
        delivered = list(prices.columns)
        if self.all_eligible:
            since = self._history_start(delivered, raw)
            assets = [self._asset_info(self.instruments[t], since[t], v2=True) for t in delivered]
        else:
            assets = [self._asset_info(self.instruments[t]) for t in delivered]
        rf, rf_method = self._risk_free(prices)

        self.last_report = SnapshotReport(
            as_of=as_of, source=self.source.name, data_source=self.source.data_source,
            requested=requested, delivered=delivered, dropped=dropped, n_rows=len(prices),
            first_date=prices.index.min().date(), last_date=prices.index.max().date(),
            price_sources=self._provenance(delivered),
            filled_values={t: n for t, n in filled.items() if t in delivered and n},
            risk_free_rate_annual=rf, risk_free_method=rf_method, alerts=self._drawdown_alerts(prices),
            **self._data_labels(delivered),
        )
        snapshot = MarketSnapshot(as_of=as_of, prices=prices, assets=assets,
                                  risk_free_rate_annual=rf, data_source=self.source.data_source)
        if len(self._snapshot_memo) >= self.SNAPSHOT_MEMO_SIZE:
            self._snapshot_memo.pop(next(iter(self._snapshot_memo)))
        self._snapshot_memo[self._memo_key(requested, as_of, lookback_days)] = (self._copy_snapshot(snapshot), self.last_report)
        return snapshot

    # ---- the whole crypto market (crypto-eligible investors): briefings only, not Agent A ----
    def get_crypto_market(self, as_of: date, lookback_days: int = 90, workers: int = 4,
                          alert_top_n: int = 20) -> CryptoMarketReport:
        """Prices for every Binance USDT pair in the catalog: changes and drawdowns, by volume.

        Drawdown alerts only for coins in the investor's universe and the alert_top_n most traded:
        most small coins are 10% off a recent high at any time, and alerts must mean material change.
        """
        from concurrent.futures import ThreadPoolExecutor

        pairs = [i for i in self.catalog if i.market == "crypto_spot" and i.asset_class == "crypto"]
        if not self._crypto_eligible(self.interests):
            raise ValueError("the crypto market report is only for investors allowed to hold crypto on Binance")
        start = as_of - timedelta(days=lookback_days + 10)
        with ThreadPoolExecutor(max_workers=workers) as pool:   # one ticker per request, as Binance serves them
            frames = list(pool.map(lambda t: self.source.fetch_adjusted_close([t], start, as_of),
                                   [i.ticker for i in pairs]))
        raw = pd.concat([f for f in frames if not f.empty], axis=1, sort=True) if any(not f.empty for f in frames) \
            else pd.DataFrame(index=pd.DatetimeIndex([]))
        saved, self.min_history_rows = self.min_history_rows, 2
        try:
            prices, dropped, _ = self._clean(raw, [i.ticker for i in pairs], as_of, trim_late=False, align=False)
        finally:
            self.min_history_rows = saved
        rank = {i.ticker: n + 1 for n, i in enumerate(pairs)}
        entries = []
        for ticker in sorted(prices.columns, key=lambda t: rank[t]):
            s = prices[ticker].dropna()
            change = lambda k: float(s.iloc[-1] / s.iloc[-1 - k] - 1.0) if len(s) > k else None
            entries.append(CryptoMarketEntry(
                ticker=ticker, base=self.instruments[ticker].aliases[0] if self.instruments[ticker].aliases else ticker,
                volume_rank=rank[ticker], quote_volume_24h=self.crypto_volumes.get(ticker),
                last_close=float(s.iloc[-1]), change_1d=change(1), change_30d=change(30), change_90d=change(90),
                drawdown_from_peak=float(s.iloc[-1] / s.max() - 1.0)))
        watched = [t for t in prices.columns if rank[t] <= alert_top_n or t in self.selection.tickers]
        return CryptoMarketReport(as_of=as_of, n_pairs_listed=len(pairs), entries=entries, dropped=dropped,
                                  alerts=self._drawdown_alerts(prices[watched]) if watched else [])

    def _crypto_eligible(self, interests: InvestorInterests) -> bool:
        """Allowed to hold crypto spot (R5, not knowledge-blocked), trades on Binance, and asked for crypto."""
        on_binance = not interests.trading_platforms or "binance" in interests.trading_platforms
        return ((interests.max_r_level is None or interests.max_r_level >= 5)
                and "crypto_spot" not in interests.blocked_product_types and on_binance
                and bool({"crypto", "altcoins"} & set(interests.interest_areas)))

    def _with_binance_market(self, catalog: list[Instrument], market: BinanceMarket) -> list[Instrument]:
        """The catalog with every Binance USDT coin as an "altcoins" instrument, most traded first."""
        listed = market.ranked_pairs()
        self.crypto_volumes = {symbol: vol for symbol, _, vol in listed}
        known = {i.ticker: i for i in catalog}
        majors = {i.ticker for i in catalog if "crypto" in i.areas}            # BTC and ETH keep their area
        coins = [known[s] if s in known else Instrument(
                     ticker=s, name=f"{base} / Tether (Binance spot)", asset_class="crypto", product_type="crypto_spot",
                     r_level=5, areas=["altcoins"], market="crypto_spot", aliases=[base, f"{base}/USDT"],
                     expense_ratio=0.0, trading_cost_bps=10.0, synthetic_mu=0.10, synthetic_vol=0.90,
                     synthetic_equity_loading=0.40)
                 for s, base, _ in listed if s not in majors]
        alt_tickers = {i.ticker for i in coins}
        rest = [i for i in catalog if i.ticker not in alt_tickers and not ("altcoins" in i.areas and i.asset_class == "crypto")]
        return rest + coins

    # ---- watchlist (crypto/gold): prices for briefings and alerts, not for Agent A ----
    def get_price_history(self, asset_ids: list[str], as_of: date, lookback_days: int) -> pd.DataFrame:
        """Adjusted closes of `asset_ids` over up to `lookback_days` trading days, EACH ASSET ITS OWN HISTORY.

        Unlike get_market_snapshot, assets are not cut to a common start and late listings are not
        dropped: a fund listed in 2024 keeps its NaNs before then. Agent A's goal simulation completes
        short histories itself (agents/quant/goal.py, complete_history) instead of throwing away years of
        everyone else's data. Same cleaning otherwise (point-in-time cut, exchange calendar, gaps, staleness).
        """
        unknown = sorted(set(asset_ids) - set(self.instruments))
        if unknown:
            raise ValueError(f"unknown assets: {unknown}")
        start = self.window_start(as_of, lookback_days)
        tickers = list(asset_ids)
        cached = self._cache
        if cached is not None and set(tickers) <= cached[0] and cached[1] <= start and as_of <= cached[2]:
            raw = cached[3].loc[pd.Timestamp(start):pd.Timestamp(as_of), tickers].copy()   # fetch_universe's data
        else:
            # NOT _fetch: a failure here must not switch the whole run to other prices (the portfolios
            # were built on these); fall back to the history already in memory instead, shorter or not.
            try:
                raw = self.source.fetch_adjusted_close(tickers, start, as_of)
            except PriceSourceError:
                raw = None
            if raw is None or not set(tickers) <= set(raw.columns):
                if cached is None or not set(tickers) <= cached[0]:
                    raise PriceSourceError("no price history for the goal simulation")
                raw = cached[3].loc[:pd.Timestamp(as_of), tickers].copy()
        prices, dropped, _ = self._clean(raw, tickers, as_of, trim_late=False, align=False)
        # An old gap (e.g. a trading halt years ago) must not cost a fund its whole history: keep its
        # longest recent gap-free stretch instead.
        for d in dropped:
            if not d.reason.startswith("price gap") or d.ticker not in raw.columns:
                continue
            s = raw[d.ticker].copy()
            s.index = pd.to_datetime(s.index)
            s = s.loc[: pd.Timestamp(as_of)].reindex(prices.index).ffill(limit=self.max_fill_days)
            gaps = s[s.isna() & (s.index > s.first_valid_index())] if s.notna().any() else s
            recent = s.loc[gaps.index.max():].dropna() if len(gaps) else s.dropna()
            if len(recent) >= self.min_history_rows:
                prices[d.ticker] = recent
        return prices

    def get_watchlist_snapshot(self, as_of: date, lookback_days: int = 90) -> SnapshotReport:
        """Prices of the watchlist assets (e.g. BTCUSDT from Binance). Returns a report with alerts.

        The cleaned prices are in self.last_watchlist_prices. Nothing here is passed to Agent A.
        """
        tickers = self.selection.watchlist_tickers
        start = as_of - timedelta(days=lookback_days + 10)
        raw = self._fetch(tickers, start, as_of) if tickers else pd.DataFrame()
        saved, self.min_history_rows = self.min_history_rows, 2
        try:
            prices, dropped, filled = self._clean(raw, tickers, as_of, trim_late=False)
        finally:
            self.min_history_rows = saved
        prices = prices.tail(lookback_days + 1)
        self.last_watchlist_prices = prices
        delivered = list(prices.columns)
        return SnapshotReport(
            as_of=as_of, source=self.source.name, data_source=self.source.data_source, requested=tickers,
            delivered=delivered, dropped=dropped, n_rows=len(prices),
            first_date=prices.index.min().date() if len(prices) else None,
            last_date=prices.index.max().date() if len(prices) else None,
            price_sources=self._provenance(delivered),
            filled_values={t: n for t, n in filled.items() if t in delivered and n},
            risk_free_rate_annual=0.0, risk_free_method="not used for the watchlist",
            alerts=self._drawdown_alerts(prices) if len(prices) else [],
            **self._data_labels(delivered),
        )

    def export_prices(self, start: date, end: date, path: str | None = None) -> pd.DataFrame:
        """Raw prices of the universe and watchlist, optionally saved to a CSV that CsvPriceSource can
        replay offline (e.g. for a live demo where Binance or the internet is not reachable)."""
        frame = self._fetch(self.selection.tickers + self.selection.watchlist_tickers, start, end)
        if path is not None:
            frame.rename_axis("date").reset_index().to_csv(path, index=False)
        return frame

    # ---- helpers for the backtest and the Advisor ----
    def preload(self, start: date, end: date) -> None:
        """Fetch the whole universe once; later snapshots inside [start, end] are served from memory."""
        self._fetch(self.selection.tickers, start, end)

    def price_table(self, start: date, end: date) -> tuple[pd.DataFrame, list[AssetInfo]]:
        """Clean, aligned prices for the whole universe, for egoquant.agents.quant.backtest (HistoricalMarketProvider)."""
        raw = self._fetch(self.selection.tickers, start, end)
        prices, _, _ = self._clean(raw, self.selection.tickers, end)
        return prices, [self._asset_info(self.instruments[t]) for t in prices.columns]

    # ---- internals ----
    @staticmethod
    def window_start(as_of: date, lookback_days: int) -> date:
        """First calendar day to fetch: ~7 calendar days per 5 trading days, plus a buffer for holidays."""
        return as_of - timedelta(days=int(lookback_days * 7 / 5) + 30)

    def _within_tiingo_quota(self, budget: dict[str, int]) -> dict[str, int]:
        """Method v2 asks for ~300 products, so the new Tiingo requests of one build are also capped by what is left of
        the free tier's hourly allowance on the traders' key (50 an hour, minus what this process already sent).
        Prices already on disk or in the snapshot cost nothing, whatever the budget."""
        left = max(0, TIINGO_HOURLY_REQUESTS - tiingo_requests_in_last(3600.0, settings.tiingo_api_key() or None))
        if "us_listed" in budget:
            budget["us_listed"] = min(budget["us_listed"], left)
        return budget

    def interest_assets(self, per_area: int | None = None) -> set[str]:
        """The products Agent A must keep through its screening under method v2: holdings plus the products that match
        one of the investor's interest areas (core funds are single exposures Agent A's screen may merge).

        With a broad interest ("stocks", "technology") the matches are dozens of products and would keep the whole
        universe; `per_area=3` keeps only the 3 most relevant and most traded of each area (the order of
        `selection.queues`: matches several interests first, then the most traded) besides the holdings."""
        held = {i.ticker for i in self.selection.instruments if "held" in i.reasons}
        matching = {i.ticker for i in self.selection.instruments if any(r.startswith("interest:") for r in i.reasons)}
        if per_area is None:
            return held | matching
        top = {t for area in self.selection.interest_areas for t in self.selection.queues.get(area, [])[:per_area]}
        return held | (top & matching)

    def _needs_request(self, ticker: str, start: date, end: date) -> bool:
        """Whether pricing `ticker` costs a request (cached and offline sources: no)."""
        check = getattr(self.source, "needs_request", None)
        return bool(check(ticker, start, end)) if callable(check) else False

    def _allows_new(self, ticker: str) -> bool:
        """False when a new ticker would break the source's monthly limit of distinct tickers (Tiingo)."""
        check = getattr(self.source, "allows_new", None)
        return bool(check(ticker)) if callable(check) else True

    def _fetch(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        if self._cache is not None:
            cached_tickers, c_start, c_end, frame = self._cache
            if set(tickers) <= cached_tickers and c_start <= start and end <= c_end:
                present = [t for t in tickers if t in frame.columns]
                return frame.loc[pd.Timestamp(start):pd.Timestamp(end), present].copy()
        try:
            if isinstance(self.source, SnapshotPriceSource) and \
                    (end - self.source.prices_as_of).days > self.max_snapshot_age_days:
                raise PriceSourceError(f"the price snapshot runs to {self.source.prices_as_of}, more than "
                                       f"{self.max_snapshot_age_days} days before {end}")
            frame = self.source.fetch_adjusted_close(tickers, start, end)
        except PriceSourceError as exc:
            # The WHOLE run switches to the next source, so prices from two sources are never mixed,
            # and the labels say which one was used (SnapshotReport.price_note / data_source).
            self.source = self._replacement(self.source, exc)
            self._cache = None
            return self._fetch(tickers, start, end)
        self._cache = (frozenset(tickers), start, end, frame)
        return frame.copy()

    def _snapshot_usable(self, as_of: date) -> bool:
        """Whether the run could switch to the committed snapshot now (on, not in use yet, readable, recent)."""
        if not self.use_snapshot or isinstance(self.source, (SnapshotPriceSource, SyntheticPriceSource)):
            return False
        try:
            snapshot = load_snapshot(self.snapshot_path)
        except PriceSourceError:
            return False
        return (as_of - snapshot.prices_as_of).days <= self.max_snapshot_age_days

    def _replacement(self, failed: PriceSource | None, exc: PriceSourceError) -> PriceSource:
        """The next source after `failed` (None = the live feed could not even be set up):
        live -> committed snapshot of REAL prices -> SYNTHETIC (only if EGOQUANT_DATA_FALLBACK=synthetic).
        Raises when nothing is left. fallback_reason keeps the FIRST failure (the live feed's)."""
        candidates: list[PriceSource] = []
        if self.use_snapshot and not isinstance(failed, (SnapshotPriceSource, SyntheticPriceSource)):
            for path in self._snapshot_paths:          # the Yahoo file, else the Tiingo one if it cannot be read
                try:
                    candidates.append(load_snapshot(path))
                    break
                except PriceSourceError:
                    continue
        if self.fallback_to_synthetic and getattr(failed, "data_source", None) != "synthetic":
            candidates.append(SyntheticPriceSource(catalog=self.catalog))
        if not candidates:   # keep the live feed's reason first: it is what went wrong
            raise exc if self.fallback_reason is None else PriceSourceError(f"{self.fallback_reason}; {exc}")
        self.fallback_reason = self.fallback_reason or str(exc)
        return candidates[0]

    def _snapshot_for_snapshot_mode(self) -> SnapshotPriceSource | None:
        """The committed snapshot to use as THE source in snapshot price mode, or None (live feeds are used, with a note)
        when snapshots are switched off, none can be read, or the newest is older than `max_snapshot_age_days`."""
        if not self.use_snapshot:
            self.notes.append("snapshot price mode is on but the snapshot is switched off (EGOQUANT_PRICE_SNAPSHOT): "
                              "live prices are used")
            return None
        for path in self._snapshot_paths:
            try:
                snapshot = load_snapshot(path)
            except PriceSourceError:
                continue
            if (date.today() - snapshot.prices_as_of).days > self.max_snapshot_age_days:
                self.notes.append(f"snapshot price mode is on but the saved prices end {snapshot.prices_as_of}, more than "
                                  f"{self.max_snapshot_age_days} days ago: live prices are used")
                return None
            return snapshot
        self.notes.append("snapshot price mode is on but no snapshot could be read: live prices are used")
        return None

    def _data_labels(self, delivered: list[str] | None = None) -> dict:
        """How the prices were sourced, for SnapshotReport: fallback reason, snapshot date, notes. `delivered` = the instruments
        actually priced in this report: the price note names only the feeds of THOSE (no "crypto from Binance" without a crypto pair)."""
        notes = list(self.notes)
        for market, reason in getattr(self.source, "failures", {}).items():
            notes.append(f"{market.replace('_', ' ')} prices could not be fetched ({reason}); those assets were "
                         "left out, and every other price is real")
        snapshot = self.source if isinstance(self.source, SnapshotPriceSource) else None
        chosen = bool(snapshot) and self.snapshot_first and self.fallback_reason is None
        return dict(fallback_reason=self.fallback_reason, notes=notes,
                    prices_as_of=snapshot.prices_as_of if snapshot else None,
                    price_note=((snapshot.calm_label_for(delivered) if chosen else snapshot.label_for(delivered)) if snapshot else None),
                    snapshot_first=chosen)

    def _missing_reason(self, ticker: str) -> str:
        inst = self.instruments.get(ticker)
        failures = getattr(self.source, "failures", {})
        if inst is not None and inst.market in failures:
            return f"{inst.market.replace('_', ' ')} prices unavailable: {failures[inst.market]}"
        return "no data from the source"

    def _clean(self, raw: pd.DataFrame, requested: list[str], as_of: date, trim_late: bool = True,
               align: bool = True) -> tuple[pd.DataFrame, list[DroppedAsset], dict[str, int]]:
        """Point-in-time cut, de-duplication, gap filling, staleness and history checks.

        trim_late: drop a non-core asset whose prices start well after the core funds' (a recent
        listing), instead of cutting every other asset's history to its start.
        align: cut every asset to the common start date (needed for Agent A's covariance). False keeps
        each asset's own history (market reports), so one new listing does not shorten the rest.
        """
        dropped: list[DroppedAsset] = []
        # Snapshot prices are frozen on its date: staleness is judged from there (the label says so).
        stale_ref = min(as_of, self.source.prices_as_of) if isinstance(self.source, SnapshotPriceSource) else as_of
        frame = raw.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            frame.index = pd.to_datetime(frame.index)
        if frame.index.tz is not None:
            frame.index = frame.index.tz_localize(None)
        frame = frame.sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = frame.loc[: pd.Timestamp(as_of)]                       # no look-ahead, ever
        frame = frame.apply(pd.to_numeric, errors="coerce").where(lambda x: x > 0)  # bad ticks -> missing
        frame = frame.dropna(how="all")                                # weekends / empty rows
        # Mixed calendars: crypto trades every day, ETFs on exchange business days. Keep only the days
        # an exchange-listed asset traded, so ETFs get no fake zero-return weekend rows.
        exchange_cols = [t for t in frame.columns if t in self.instruments and not self.instruments[t].trades_24_7]
        if exchange_cols and len(exchange_cols) < len(frame.columns):
            frame = frame[frame[exchange_cols].notna().any(axis=1)]

        kept: list[str] = []
        for ticker in requested:
            if ticker not in frame.columns or frame[ticker].notna().sum() == 0:
                dropped.append(DroppedAsset(ticker=ticker, reason=self._missing_reason(ticker)))
                continue
            last_valid = frame[ticker].last_valid_index().date()
            if (stale_ref - last_valid).days > self.stale_days:
                dropped.append(DroppedAsset(ticker=ticker, reason=f"stale: last price on {last_valid}"))
                continue
            kept.append(ticker)

        frame = frame[kept]
        before = frame.isna()
        frame = frame.ffill(limit=self.max_fill_days)
        filled = {t: int((before[t] & frame[t].notna()).sum()) for t in kept}

        final: list[str] = []
        for ticker in kept:
            series = frame[ticker].loc[frame[ticker].first_valid_index():]
            if series.isna().any():
                dropped.append(DroppedAsset(ticker=ticker,
                                            reason=f"price gap longer than {self.max_fill_days} days"))
            elif len(series) < self.min_history_rows:
                dropped.append(DroppedAsset(ticker=ticker, reason=f"only {len(series)} days of history; "
                                                                  f"need at least {self.min_history_rows}"))
            else:
                final.append(ticker)

        core = [t for t in final if t in self.instruments and self.instruments[t].core]
        if trim_late and core:
            core_start = max(frame[t].first_valid_index() for t in core)
            late = [t for t in final if t not in core and frame[t].first_valid_index() > core_start + pd.Timedelta(days=7)]
            for ticker in late:
                dropped.append(DroppedAsset(ticker=ticker, reason=f"prices start {frame[ticker].first_valid_index().date()}, "
                                                                  "later than the rest of the universe; keeping it would "
                                                                  "shorten every estimate"))
            final = [t for t in final if t not in late]

        frame = frame[final]
        if final and align:
            common_start = max(frame[t].first_valid_index() for t in final)
            frame = frame.loc[common_start:].dropna()
        frame.index.name = "date"
        return frame, dropped, filled

    def _provenance(self, tickers: list[str]) -> dict[str, str]:
        """Which feed priced each asset, e.g. {"BTCUSDT": "binance", "VT": "tiingo"}."""
        if isinstance(self.source, RoutedPriceSource):
            return {t: getattr(self.source.source_for(t), "name", "none") for t in tickers}
        return {t: self.source.name for t in tickers}

    @staticmethod
    def _asset_info(inst: Instrument, v2_since: date | None = None, v2: bool = False) -> AssetInfo:
        """v2=True (portfolio method v2) also fills area, region, sector, history_start and product_type; v1 is unchanged."""
        extra = dict(area=inst.areas[0] if inst.areas else None, region=region_of(inst), sector=inst.sector,
                     history_start=v2_since, product_type=inst.product_type) if v2 else {}
        return AssetInfo(
            asset_id=inst.ticker, asset_class=inst.asset_class,  # type: ignore[arg-type]
            currency="USD", expense_ratio=inst.expense_ratio, trading_cost_bps=inst.trading_cost_bps,
            duration_years=inst.duration_years if inst.asset_class == "bond" else None,
            convexity=inst.convexity if inst.asset_class == "bond" else None,
            liquidity_tier=inst.liquidity_tier, max_weight=inst.max_weight, **extra,
        )

    def _history_start(self, tickers: list[str], raw: pd.DataFrame) -> dict[str, date | None]:
        """The first price date Agent C holds for each product: from the long history fetch_universe kept (the goal
        simulation's ~20 years), else from the frame just fetched."""
        long_frame = self._cache[3] if self._cache is not None else None
        out: dict[str, date | None] = {}
        for t in tickers:
            first = None
            for frame in (long_frame, raw):
                if frame is not None and t in frame.columns:
                    index = frame[t].first_valid_index()
                    if index is not None and (first is None or index < first):
                        first = index
            out[t] = first.date() if first is not None else None
        return out

    def _risk_free(self, prices: pd.DataFrame) -> tuple[float, str]:
        if self.risk_free_rate_annual is not None:
            return self.risk_free_rate_annual, "fixed by configuration"
        cash = [t for t in prices.columns if self.instruments[t].asset_class == "cash"]
        if cash and len(prices) >= 64:
            ticker = cash[0]
            k = min(252, len(prices) - 1)
            series = prices[ticker].iloc[-(k + 1):]
            rate = float((series.iloc[-1] / series.iloc[0]) ** (252.0 / k) - 1.0)
            return rate, f"trailing {k}-day return of {ticker}, annualized (historical, not a forecast)"
        return self.fallback_risk_free_rate, "fallback constant (no cash fund with enough history)"

    def _drawdown_alerts(self, prices: pd.DataFrame) -> list[DrawdownAlert]:
        window = prices.tail(self.drawdown_window_days + 1)
        if len(window) < 2:
            return []
        alerts = []
        for ticker in prices.columns:
            drawdown = float(window[ticker].iloc[-1] / window[ticker].max() - 1.0)
            if drawdown <= -self.drawdown_alert_threshold:
                alerts.append(DrawdownAlert(
                    ticker=ticker, window_days=len(window) - 1, drawdown_from_peak=drawdown,
                    message=(f"{ticker} closed {abs(drawdown):.1%} below its highest close of the last "
                             f"{len(window) - 1} trading days (data to {window.index[-1].date()}). "
                             "This is what changed, not a forecast or a sell signal; the Advisor may offer "
                             "a risk-profile re-check."),
                ))
        return alerts
