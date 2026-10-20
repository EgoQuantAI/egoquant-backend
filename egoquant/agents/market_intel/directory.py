"""Agent C's investment directory: every US-listed stock and ETF, sorted into interest-area clusters.

Names and descriptive data come first, prices later and only for what an investor asks about:

    Nasdaq stock screener     every listed stock: name, sector, industry, market cap, country (1 request)
    Nasdaq symbol directory   every listed security with its ETF flag (2 text files)
    Tiingo supported tickers  which tickers Tiingo can price and since when (1 zip file, no API key)
    Tiingo IEX top-of-book    latest volume and price of every ticker (1 API request): dollar volume
                              says how commonly traded, i.e. how investable, an instrument is

build_directory() turns those into Instrument entries with interest areas, a product type and an
R-level, ranked inside each cluster by dollar volume (stocks without IEX data by market cap). Agent C
then walks the clusters an investor picked, most traded first (see agent.py, fetch_universe).

Classification is DETERMINISTIC (fixed rules below, no LLM):
  * stocks: Nasdaq sector -> industry area (REITs filed under Finance go to real_estate); China and
    Hong Kong listings also go to "china", other non-US companies to "international_stocks", and
    companies below SMALL_CAP_LIMIT to "small_caps". All single stocks are R4 (catalog rule).
  * ETFs: keywords in the fund name. Leveraged, inverse, single-stock, options-strategy, volatility
    and ETN products are EXCLUDED (complex products, open decision 12); a name no rule matches is left
    out rather than guessed.
Every threshold is PROVISIONAL. Nasdaq's sector labels are its own (e.g. some lab-equipment makers are
"Industrials"); they are used as published. Check each source's terms of use against the competition's
"properly licensed data" rule.

The directory is saved to the cache directory and rebuilt when older than MAX_AGE_DAYS. Without a
network (or a Tiingo key for the volume ranking) Agent C keeps using the curated catalog alone.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, Field

from egoquant import settings
from egoquant.agents.market_intel.catalog import SINGLE_STOCK_MAX_WEIGHT, Instrument

logger = logging.getLogger(__name__)

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NASDAQ_SYMBOLS_URLS = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                       "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
TIINGO_TICKERS_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
TIINGO_IEX_URL = "https://api.tiingo.com/iex/"
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/130.0 Safari/537.36", "Accept": "application/json, text/plain, */*"}

MAX_AGE_DAYS = 7                     # PROVISIONAL: rebuild the directory weekly
VOLUME_SOURCE = "Tiingo IEX volume"
RULES_VERSION = "2026-10-18.6"        # bump when a classification rule changes: saved directories are rebuilt
MIN_HISTORY_DAYS = 400               # PROVISIONAL: calendar days of Tiingo history (Agent A needs ~253 trading days)
MIN_MARKET_CAP = 300e6               # PROVISIONAL: smaller companies are left out (hard to trade, thin data)
SMALL_CAP_LIMIT = 2e9                # PROVISIONAL: below this a company also counts as a "small_caps" choice
MIN_DOLLAR_VOLUME = 1e6              # PROVISIONAL: less than $1m traded a day -> left out (when volume is known)
US_EXCHANGES = {"NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "BATS", "AMEX"}


class DirectoryEntry(BaseModel):
    """One classified instrument, before it becomes an Instrument for Agent C."""

    ticker: str
    name: str
    kind: str                                          # "stock" | "etf"
    areas: list[str]
    asset_class: str
    product_type: str
    r_level: int
    sector: str | None = None
    duration_years: float | None = None                # bond funds, estimated from the name
    market_cap: float | None = None
    dollar_volume: float | None = None                 # latest day's volume x price (Tiingo IEX)
    country: str | None = None
    exposure_group: str | None = None                  # funds tracking the same index (see etf_signature)


class Directory(BaseModel):
    built_on: date
    rules_version: str = ""                            # a saved directory built with older rules is rebuilt
    entries: list[DirectoryEntry]                      # ranked: most traded first
    excluded: dict[str, int] = Field(default_factory=dict)   # reason -> how many tickers
    sources: list[str] = Field(default_factory=list)

    def clusters(self) -> dict[str, list[str]]:
        """Interest area -> tickers, most traded first."""
        out: dict[str, list[str]] = {}
        for e in self.entries:
            for area in e.areas:
                out.setdefault(area, []).append(e.ticker)
        return out


# ==========================================================================
# Stocks: Nasdaq sector -> industry area
# ==========================================================================
NASDAQ_SECTORS: dict[str, str] = {
    "Technology": "technology", "Health Care": "healthcare", "Finance": "financials", "Energy": "energy",
    "Consumer Discretionary": "consumer", "Consumer Staples": "consumer", "Industrials": "industrials",
    "Utilities": "utilities", "Telecommunications": "communication", "Basic Materials": "materials",
    "Real Estate": "real_estate",
}
# Nasdaq's sector labels are its own; a few industries clearly belong elsewhere (first match wins).
# The curated catalog's sectors still win for the names it lists (merge_catalog).
INDUSTRY_OVERRIDES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"real estate|reit", re.IGNORECASE), "real_estate"),
    (re.compile(r"semiconductor|software|computer|edp services|electronic components|computer communications",
                re.IGNORECASE), "technology"),
    (re.compile(r"pharmaceutical|biotechnology|medical|health|hospital", re.IGNORECASE), "healthcare"),
    (re.compile(r"oil & gas|oil and gas|integrated oil|oilfield|coal", re.IGNORECASE), "energy"),
    (re.compile(r"major banks|savings institutions|investment bankers|finance: consumer services|"
                r"property-casualty|life insurance|finance companies", re.IGNORECASE), "financials"),
]
_NOT_COMMON_STOCK = re.compile(r"\b(warrants?|rights?|units?|preferred|depositary shares? representing|notes?|"
                               r"debentures?|bonds?|subordinated|trust preferred)\b|%", re.IGNORECASE)


def classify_stock(name: str, sector: str, industry: str, country: str, market_cap: float | None
                   ) -> tuple[list[str], str] | str:
    """(areas, sector) for a common stock, or the reason it is left out."""
    if _NOT_COMMON_STOCK.search(name):
        return "not a common share (warrant, unit, preferred or debt)"
    area = NASDAQ_SECTORS.get(sector)
    area = next((a for pattern, a in INDUSTRY_OVERRIDES if pattern.search(industry)), area)
    if area is None:
        return "no industry from Nasdaq"
    if market_cap is None or market_cap < MIN_MARKET_CAP:
        return "market value below the minimum"
    areas = [area]
    if country in ("China", "Hong Kong"):
        areas.append("china")
    elif country and country != "United States":
        areas.append("international_stocks")
    if market_cap < SMALL_CAP_LIMIT:
        areas.append("small_caps")
    return areas, area


# ==========================================================================
# ETFs: fund name -> category (first matching rule wins)
# ==========================================================================
_EXCLUDE_ETF = [
    (re.compile(r"\b[1-9](\.\d+)?x\b|\b-[1-9]x\b|ultrapro|ultrashort|proshares ultra|leveraged|\binverse\b|\bbear\b|"
                r"\bdaily\b.*\b(bull|bear|long|short)\b|\b(bull|bear|long|short)\b.*\bdaily\b|proshares short|efficient core|\befficient\b.*\bplus\b|return stacked|\bstacked\b",
                re.IGNORECASE), "leveraged, inverse or single-stock product"),
    (re.compile(r"buffer|defined outcome|defined protection|downside protection|principal protect|capital protect|structured|covered call|\boptions?\b|premium income|enhanced income|autocallable|"
                r"barrier|\bput\b|collar|yieldmax|target income|income strategy|high income|option income|"
                r"\bboost\b", re.IGNORECASE),
     "options-based strategy"),
    (re.compile(r"\bvix\b|volatility(?! weighted)|managed futures|long/short|market neutral|tail risk|\bhedge fund",
                re.IGNORECASE), "volatility or hedge-fund style strategy"),
    (re.compile(r"\betns?\b|exchange traded notes?", re.IGNORECASE), "exchange-traded note (issuer credit risk)"),
    (re.compile(r"\btarget date\b|\b20[3-7]\d\b.*\b(retirement|target)\b|\bterm\b.*\b20[2-4]\d\b|"
                r"\b20[2-4]\d\b.*\b(maturity|term)\b", re.IGNORECASE), "fixed-maturity or target-date fund"),
]

_COUNTRIES = ("japan|india|brazil|germany|united kingdom|\\buk\\b|canada|south korea|korea|taiwan|mexico|"
              "australia|france|switzerland|italy|spain|netherlands|sweden|israel|saudi|indonesia|vietnam|"
              "south africa|singapore|hong kong|chile|poland|turkey|thailand|philippines|malaysia|argentina")

# (pattern, areas, asset_class, product_type, r_level, sector)
_ETF_RULES: list[tuple[str, list[str], str, str, int, str | None]] = [
    # funds of crypto-INDUSTRY companies are equity (technology), not crypto-assets
    (r"crypto.*(industry|equity|stocks|companies|innovators|economy|thematic|miners)|blockchain|bitcoin miners|"
     r"digital (assets|economy)", ["technology"], "equity", "equity_etf", 4, "technology"),
    # crypto-assets before commodities (a "Bitcoin Strategy" fund must not land in commodities)
    (r"bitcoin|\bbtc\b", ["crypto"], "crypto", "crypto_etp", 5, None),
    (r"ether\b|ethereum", ["crypto"], "crypto", "crypto_etp", 5, None),
    (r"solana|\bxrp\b|crypto", ["altcoins"], "crypto", "crypto_etp", 5, None),
    # cash-like
    # cash-like: ONLY Treasury bills and government money-market funds (Agent A treats cash as riskless)
    (r"0-3 month|1-3 month|0-1 month|treasury bill|t-bill|\bbills?\b|floating rate treasury|treasury floating|"
     r"(government|treasury|u\.?s\.? treasury) money market", ["cash"], "cash", "cash_etf", 1, None),
    # ultra-short and floating-rate CREDIT funds can lose money in a credit shock: bonds, not cash
    (r"ultra[- ]short|short[- ]duration|enhanced cash|cash management|floating rate|money market",
     ["bonds"], "bond", "bond_etf", 2, None),
    # metals and commodities (miners are equity, below)
    (r"\bgold\b(?!.*miner)(?!.*mining)", ["gold"], "commodity", "commodity_etp", 3, None),
    (r"(silver|platinum|palladium|precious metal)(?!.*miner)(?!.*mining)", ["precious_metals"], "commodity",
     "commodity_etp", 4, None),
    (r"crude|\boil fund\b|natural gas|commodit|agricultur|\bcopper\b(?!.*miner)|base metals|\bgrains?\b",
     ["commodities"], "commodity", "commodity_etp", 4, None),
    # bonds
    (r"(1-3|0-5|0-1|1-5|short[- ]term|short) .*treasur|treasur.*(1-3|0-5|0-1|short)", ["government_bonds"], "bond",
     "bond_etf", 1, None),
    (r"(20\+|25\+|10\+|long[- ]term|long|extended duration).*treasur|treasur.*(20\+|long)", ["long_term_bonds"],
     "bond", "bond_etf", 3, None),
    (r"\btips\b|inflation[- ](protected|linked|indexed)|treasury inflation", ["inflation_protected"], "bond", "bond_etf", 2, None),
    (r"emerging.*(bond|debt)|(bond|debt).*emerging", ["international_bonds"], "bond", "bond_etf", 3, None),
    (r"(international|global|world|ex-us|non-us).*(bond|treasur|fixed income|aggregate)", ["international_bonds"],
     "bond", "bond_etf", 2, None),
    (r"treasur|government bond|govt", ["government_bonds"], "bond", "bond_etf", 2, None),
    (r"high yield|junk|senior loan|bank loan|\bclo\b|fallen angel", ["corporate_bonds"], "bond", "bond_etf", 3, None),
    (r"corporate|investment grade|credit", ["corporate_bonds"], "bond", "bond_etf", 2, None),
    (r"municipal|\bmuni\b|aggregate|total bond|core bond|core plus|bond|fixed income|mortgage-backed|\bmbs\b",
     ["bonds"], "bond", "bond_etf", 2, None),
    # real estate
    (r"\breit\b|real estate", ["real_estate"], "equity", "reit_etf", 4, "real_estate"),
    # equity regions before sectors ("China Technology" is a China fund)
    (r"(?<!ex )(?<!ex-)\b(china|chinese)\b", ["china", "emerging_markets"], "equity", "equity_etf", 4, None),
    (r"emerging", ["emerging_markets"], "equity", "equity_etf", 4, None),
    (_COUNTRIES, ["international_stocks"], "equity", "equity_etf", 4, None),
    (r"international|developed|eafe|ex-us|ex us|europe|asia|pacific|non-us|foreign", ["stocks", "international_stocks"],
     "equity", "equity_etf", 3, None),
    # sectors
    (r"dow jones industrial average|\bdow 30\b", ["stocks", "us_stocks"], "equity", "equity_etf", 3, None),
    (r"semiconductor|software|(?<!bio)technolog|\btech\b|cyber|cloud|internet|artificial intelligence|\bai\b|robotic|"
     r"innovation|digital|blockchain", ["technology"], "equity", "equity_etf", 4, "technology"),
    (r"health|biotech|pharma|medical|genomic", ["healthcare"], "equity", "equity_etf", 4, "healthcare"),
    (r"financ|bank|insurance|broker", ["financials"], "equity", "equity_etf", 4, "financials"),
    (r"miner|mining|materials|metals|steel|chemical|lithium|uranium", ["materials"], "equity", "equity_etf", 4, "materials"),
    (r"energy|oil & gas|oil and gas|\bmlp\b|pipeline|clean energy|solar", ["energy"], "equity", "equity_etf", 4,
     "energy"),
    (r"consumer|retail|staples|discretionary|homebuild", ["consumer"], "equity", "equity_etf", 4, "consumer"),
    (r"industrial|aerospace|defense|transport|infrastructure", ["industrials"], "equity", "equity_etf", 4,
     "industrials"),
    (r"utilit", ["utilities"], "equity", "equity_etf", 4, "utilities"),
    (r"communication|media|telecom", ["communication"], "equity", "equity_etf", 4, "communication"),
    # styles and broad markets
    (r"small[- ]?cap|russell 2000|micro[- ]?cap|smallcap", ["small_caps"], "equity", "equity_etf", 4, None),
    (r"dividend|equity income|shareholder yield", ["dividend_stocks"], "equity", "equity_etf", 3, None),
    (r"world|global|all[- ]country|acwi", ["stocks"], "equity", "equity_etf", 3, None),
    (r"s&p 500|total (stock )?market|large[- ]?cap|mid[- ]?cap|russell 1000|russell 3000|nasdaq[- ]100|\bu\.?s\.?\b|"
     r"america|equity|stock|growth|value|momentum|quality|factor|low vol", ["stocks", "us_stocks"], "equity",
     "equity_etf", 3, None),
]
_ETF_PATTERNS = [(re.compile(p, re.IGNORECASE), *rest) for p, *rest in _ETF_RULES]


def classify_etf(name: str) -> DirectoryEntry | str:
    """A partly filled entry (category fields only), or the reason the fund is left out."""
    for pattern, reason in _EXCLUDE_ETF:
        if pattern.search(name):
            return reason
    for pattern, areas, asset_class, product_type, r_level, sector in _ETF_PATTERNS:
        if pattern.search(name):
            return DirectoryEntry(ticker="", name=name, kind="etf", areas=list(areas), asset_class=asset_class,
                                  product_type=product_type, r_level=r_level, sector=sector,
                                  duration_years=bond_duration(name) if asset_class == "bond" else None,
                                  exposure_group=etf_signature(name))
    return "category not recognisable from the name"


def bond_duration(name: str) -> float:
    """APPROXIMATE duration from the maturity band in the name (Agent A's rate stress needs one)."""
    n = name.lower()
    for pattern, years in [(r"0-3 month|1-3 month|0-1 month|bill|floating|ultra", 0.2), (r"0-5|1-3|1-5|short", 2.0),
                           (r"3-7|5-10|7-10|intermediate", 6.0), (r"10-20", 12.0),
                           (r"20\+|25\+|long|extended", 17.0)]:
        if re.search(pattern, n):
            return years
    return 6.0


_INDEX_KEYS = ["municipal", "s&p 500", "total stock market", "total market", "nasdaq-100", "nasdaq 100", "russell 2000",
               "russell 1000", "russell 3000", "eafe", "emerging markets", "aggregate", "total bond",
               "20+ year treasury", "1-3 year treasury", "7-10 year treasury", "0-3 month", "1-3 month", "tips",
               "gold", "silver", "bitcoin", "ether", "real estate", "high yield"]
_MODIFIERS = ["equal weight", "growth", "value", "dividend", "momentum", "quality", "low vol", "esg", "hedged",
              "small", "mid", "miner", "strategy", "futures", "ex-", "international", "global", "short", "0-5",
              "1-5"]


def etf_signature(name: str) -> str | None:
    """Funds tracking the same index get the same signature (e.g. VOO, IVV, SPY: "s&p 500")."""
    n = name.lower().replace("nasdaq 100", "nasdaq-100").replace("total market", "total stock market")
    n = re.sub(r"\bmuni\b|tax[- ]exempt", "municipal", n)
    key = next((k for k in _INDEX_KEYS if k in n), None)
    if key is None:
        return None
    mods = sorted(m for m in _MODIFIERS if m in n)
    return "|".join([key, *mods])


# ==========================================================================
# Build (I/O in the loaders; the rest is pure)
# ==========================================================================
def build_directory(stock_rows: list[dict], symbol_rows: list[dict], tiingo_rows: list[dict],
                    iex_rows: list[dict] | None, today: date,
                    known_volume: dict[str, float] | None = None) -> Directory:
    """Pure: the four sources -> a ranked, classified directory of what Tiingo can price."""
    excluded: dict[str, int] = {}

    def skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    cutoff_start = (today - timedelta(days=MIN_HISTORY_DAYS)).isoformat()
    recent = (today - timedelta(days=10)).isoformat()
    on_tiingo: set[str] = set()
    for r in tiingo_rows:
        if (r.get("priceCurrency") == "USD" and r.get("exchange") in US_EXCHANGES
                and (r.get("endDate") or "") >= recent and (r.get("startDate") or "9") <= cutoff_start):
            on_tiingo.add(r["ticker"].upper())
    dollar_volume = {}
    for r in iex_rows or []:
        price = r.get("prevClose") or r.get("tngoLast") or r.get("last")
        if r.get("volume") and price:
            dollar_volume[str(r["ticker"]).upper()] = float(r["volume"]) * float(price)
    volume_note = VOLUME_SOURCE
    if not dollar_volume and known_volume:
        # No volume today (rate limit, outage): rank with the volumes of the last directory that had them.
        dollar_volume, volume_note = dict(known_volume), f"{VOLUME_SOURCE} (saved)"

    entries: dict[str, DirectoryEntry] = {}
    for r in stock_rows:
        ticker = _tiingo_symbol(r.get("symbol", ""))
        if not ticker or "^" in ticker:
            skip("not a common share (warrant, unit, preferred or debt)")
            continue
        cap = _number(r.get("marketCap"))
        result = classify_stock(r.get("name", ""), r.get("sector", ""), r.get("industry", ""), r.get("country", ""), cap)
        if isinstance(result, str):
            skip(result)
            continue
        areas, sector = result
        entries[ticker] = DirectoryEntry(ticker=ticker, name=_clean_name(r.get("name", ticker)), kind="stock",
                                         areas=areas, asset_class="equity", product_type="stock", r_level=4,
                                         sector=sector, market_cap=cap, country=r.get("country") or None)
    for r in symbol_rows:
        if r.get("ETF") != "Y" or r.get("Test Issue") == "Y":
            continue
        ticker = _tiingo_symbol(r.get("Symbol") or r.get("ACT Symbol") or "")
        if not ticker or ticker in entries:
            continue
        name = r.get("Security Name", "")
        result = classify_etf(name)
        if isinstance(result, str):
            skip(result)
            continue
        entries[ticker] = result.model_copy(update={"ticker": ticker, "name": name.strip()})

    kept: list[DirectoryEntry] = []
    for ticker, entry in entries.items():
        if ticker not in on_tiingo:
            skip("not priced by Tiingo (or too little history)")
            continue
        dv = dollar_volume.get(ticker)
        if dollar_volume and (dv is None or dv < MIN_DOLLAR_VOLUME):
            skip("traded less than the minimum a day")
            continue
        if not dollar_volume and entry.kind == "etf":
            # Without volume nothing says which funds are common, and the market-value ranking only covers
            # stocks; the curated catalog's funds are added back by merge_catalog.
            skip("no trading volume to rank it")
            continue
        kept.append(entry.model_copy(update={"dollar_volume": dv}))
    # Most traded first; without volume data, larger companies first.
    kept.sort(key=lambda e: (-(e.dollar_volume or 0.0), -(e.market_cap or 0.0), e.ticker))
    sources = ["Nasdaq stock screener", "Nasdaq symbol directory", "Tiingo supported tickers"]
    if dollar_volume:
        sources.append(volume_note)
    return Directory(built_on=today, rules_version=RULES_VERSION, entries=kept, excluded=excluded, sources=sources)


def to_instruments(directory: Directory, curated: list[Instrument]) -> list[Instrument]:
    """Directory entries as Instruments. A curated catalog entry wins for its ticker (better metadata:
    expense ratio, duration, exposure group), but takes the directory's place in the ranking."""
    by_ticker = {i.ticker: i for i in curated}
    # An auto-discovered fund tracking the same index as a curated group joins that group (IAUM -> gold_bullion),
    # but only when the signature points to one curated group.
    candidates: dict[str, set[str]] = {}
    for i in curated:
        signature = etf_signature(i.name)
        if i.exposure_group and signature:
            candidates.setdefault(signature, set()).add(i.exposure_group)
    curated_groups = {sig: groups.pop() for sig, groups in candidates.items() if len(groups) == 1}
    out: list[Instrument] = []
    for e in directory.entries:
        if e.ticker in by_ticker:
            out.append(by_ticker[e.ticker])
            continue
        out.append(Instrument(
            ticker=e.ticker, name=e.name, asset_class=e.asset_class, product_type=e.product_type,  # type: ignore[arg-type]
            r_level=e.r_level, areas=e.areas, sector=e.sector,  # type: ignore[arg-type]
            expense_ratio=0.0 if e.kind == "stock" else 0.004,  # APPROXIMATE: ETF fees are not in the sources
            trading_cost_bps=2.0 if e.kind == "stock" else 3.0, duration_years=e.duration_years,
            convexity=round(1.2 * e.duration_years**2, 1) if e.duration_years else None,
            max_weight=SINGLE_STOCK_MAX_WEIGHT if e.kind == "stock" else None,
            exposure_group=(curated_groups.get(e.exposure_group) or f"auto:{e.exposure_group}") if e.exposure_group else None,
            **_synthetic_defaults(e)))
    return out


def merge_catalog(directory: Directory | None, curated: list[Instrument]) -> list[Instrument]:
    """Curated core funds first (always in the universe), then the directory ranking, then curated
    instruments the directory does not have (so every interest area keeps at least its curated choices)."""
    if directory is None:
        return list(curated)
    ranked = to_instruments(directory, curated)
    seen = {i.ticker for i in ranked}
    core = [i for i in curated if i.core]
    rest = [i for i in curated if not i.core and i.ticker not in seen]
    return core + [i for i in ranked if not i.core] + rest


def _synthetic_defaults(e: DirectoryEntry) -> dict[str, float]:
    """INVENTED parameters for the offline synthetic price source only."""
    if e.asset_class == "cash":
        return {"synthetic_mu": 0.035, "synthetic_vol": 0.005, "synthetic_rate_loading": 0.05}
    if e.asset_class == "bond":
        return {"synthetic_mu": 0.035, "synthetic_vol": round(0.01 + 0.008 * (e.duration_years or 6.0), 3),
                "synthetic_rate_loading": 0.9}
    if e.asset_class == "commodity":
        return {"synthetic_mu": 0.03, "synthetic_vol": 0.2, "synthetic_equity_loading": 0.2}
    if e.asset_class == "crypto":
        return {"synthetic_mu": 0.1, "synthetic_vol": 0.6, "synthetic_equity_loading": 0.4}
    if e.kind == "stock":
        return {"synthetic_mu": 0.08, "synthetic_vol": 0.3, "synthetic_equity_loading": 0.7}
    return {"synthetic_mu": 0.07, "synthetic_vol": 0.18, "synthetic_equity_loading": 0.85}


def _tiingo_symbol(symbol: str) -> str:
    """Nasdaq writes share classes as BRK/B or BRK.B; Tiingo as BRK-B."""
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def _number(text: Any) -> float | None:
    try:
        value = float(str(text).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _clean_name(name: str) -> str:
    return re.sub(r"\s+(Common Stock|Ordinary Shares|Class [A-C] (Common Stock|Ordinary Shares)|"
                  r"American Depositary Shares.*|Common Shares)\s*$", "", name.strip(), flags=re.IGNORECASE) or name


# ---- loaders (network) ----
def fetch_nasdaq_stocks(session: Any | None = None, timeout: float = 60.0) -> list[dict]:
    s = session or requests.Session()
    r = s.get(NASDAQ_SCREENER_URL, params={"tableonly": "true", "download": "true"}, headers=BROWSER_HEADERS,
              timeout=timeout)
    r.raise_for_status()
    return r.json()["data"]["rows"]


def fetch_nasdaq_symbols(session: Any | None = None, timeout: float = 60.0) -> list[dict]:
    s = session or requests.Session()
    rows: list[dict] = []
    for url in NASDAQ_SYMBOLS_URLS:
        r = s.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        r.raise_for_status()
        lines = [ln for ln in r.text.splitlines() if ln and not ln.startswith("File Creation Time")]
        rows += list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"))
    return rows


def fetch_tiingo_tickers(session: Any | None = None, timeout: float = 120.0) -> list[dict]:
    s = session or requests.Session()
    r = s.get(TIINGO_TICKERS_URL, timeout=timeout)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(z.namelist()[0]) as fh:
            # ~130k rows worldwide; build_directory only uses US-listed USD rows. Keeping just those (and just
            # the fields it reads) holds a few MB instead of ~100 MB while the directory builds on a small server.
            keep = ("ticker", "exchange", "priceCurrency", "startDate", "endDate")
            return [{k: r.get(k) for k in keep} for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
                    if r.get("priceCurrency") == "USD" and r.get("exchange") in US_EXCHANGES]


def fetch_tiingo_iex(api_key: str | None = None, session: Any | None = None, timeout: float = 60.0) -> list[dict]:
    """One request: latest top-of-book (volume, prices) for every ticker. Needs the Tiingo key."""
    key = api_key if api_key is not None else settings.tiingo_api_key()
    if not key:
        return []
    s = session or requests.Session()
    r = s.get(TIINGO_IEX_URL, headers={"Authorization": f"Token {key}"}, timeout=timeout)
    r.raise_for_status()
    rows = r.json()
    return rows if isinstance(rows, list) else []      # an error object or nothing: treated as "no volume today"


def directory_path() -> Path:
    return settings.cache_dir() / "directory.json"


def load_directory(today: date | None = None, refresh: bool = False, path: Path | None = None) -> Directory | None:
    """The saved directory if it is fresh, else a newly built one (saved), else None (offline).

    None means: use the curated catalog only. A stale saved directory is still used when a rebuild fails.
    """
    today = today or date.today()
    path = path or directory_path()
    if settings.price_mode() == "snapshot":
        refresh = False                                 # snapshot price mode: no download, use the saved directory if any
    saved: Directory | None = None
    try:
        if path.exists():
            saved = Directory.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if settings.price_mode() == "snapshot":
        return saved                                    # whatever is saved, however old; never a request
    if (saved is not None and not refresh and saved.rules_version == RULES_VERSION
            and (today - saved.built_on).days <= MAX_AGE_DAYS):
        return saved
    try:
        iex = fetch_tiingo_iex()
    except requests.RequestException:                  # e.g. Tiingo's hourly limit (HTTP 429)
        iex = []
    known = None
    if not iex and saved is not None and (today - saved.built_on).days <= 4 * MAX_AGE_DAYS:
        known = {e.ticker: e.dollar_volume for e in saved.entries if e.dollar_volume}
    try:
        built = build_directory(fetch_nasdaq_stocks(), fetch_nasdaq_symbols(), fetch_tiingo_tickers(), iex, today,
                                known_volume=known)
    except (requests.RequestException, KeyError, ValueError, zipfile.BadZipFile):
        return saved
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(built.model_dump_json(), encoding="utf-8")
        tmp.replace(path)
    except OSError:                                    # a cache directory that cannot be written: use it unsaved
        logger.warning("The investment directory cannot be saved to the cache directory; using it unsaved")
    return built


__all__ = ["Directory", "DirectoryEntry", "build_directory", "classify_etf", "classify_stock", "etf_signature",
           "load_directory", "merge_catalog", "to_instruments"]
