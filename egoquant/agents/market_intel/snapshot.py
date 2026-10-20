"""Committed snapshots of REAL prices for the curated catalog (demo insurance).

Tiingo's free tier allows 50 requests per hour, and the disk cache is wiped on every Railway redeploy.
So the repository carries real adjusted closes, with the date they run to, in TWO files (each names every
ticker's feed; crypto pairs come from Binance in both):

    data/prices_snapshot.json.gz        Tiingo, 112 funds and crypto pairs. The REGIME MODEL reads it
                                        (egoquant/regime: the prices it was fitted and evaluated on). Keep it.
    data/prices_snapshot_yahoo.json.gz  Yahoo Finance, all 292 catalog tickers incl. the 180 single stocks.
                                        AGENT C's fallback (agent_c_snapshot_path).

One run reads only one of them, so sources are never mixed. Agent C's chain is:

    live feed  ->  its snapshot (still REAL prices, labeled "real prices as of <date>, cached")
               ->  SYNTHETIC prices, only if EGOQUANT_DATA_FALLBACK=synthetic (labeled synthetic)

Build or refresh it (needs TIINGO_API_KEY). At most 20 new Tiingo requests an hour by default, stops at
the first rate-limit error, and resumes from the disk cache when rerun:

    python -m egoquant.agents.market_intel.snapshot --as-of 2026-10-16 [--max-requests-per-hour 40]
                                                    [--years 10] [--include-stocks]

It holds the history the live goal Monte Carlo asks Agent C for (AgentConfig.goal_history_days, ~10 years
by default; never less than Agent A's lookback), so on the snapshot the simulation gets the same span as
live. --years overrides it. One Tiingo request per ticker whatever the span.

--source yahoo builds Agent C's file from Yahoo Finance instead (market_intel/yahoo.py: many tickers per request, so
the whole catalog with --include-stocks takes ~30 s; offline builds only, the app never calls Yahoo).

File format (gzip JSON): {"as_of", "start", "history_rows", "created_on", "complete", "missing", "skipped", "stopped",
"sources": {ticker: feed}, "prices": {ticker: {"YYYY-MM-DD": adjusted close}}}.
"""

from __future__ import annotations

import argparse
import functools
import gzip
import json
import time
import zlib
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, Instrument
from egoquant.agents.market_intel.sources import (
    DataSourceLabel,
    PriceSource,
    PriceSourceError,
    RateLimitError,
    build_live_source,
)

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "prices_snapshot.json.gz"   # Tiingo; the regime model's
YAHOO_SNAPSHOT_PATH = SNAPSHOT_PATH.with_name("prices_snapshot_yahoo.json.gz")          # Yahoo; Agent C's fallback
FEED_NAMES = {"tiingo": "Tiingo", "yahoo": "Yahoo Finance", "binance": "Binance"}        # as the label names a feed


def agent_c_snapshot_path() -> Path:
    """Agent C's fallback file: the Yahoo snapshot (the whole catalog, single stocks included), else the Tiingo
    one. The regime model keeps reading SNAPSHOT_PATH, the prices it was fitted and evaluated on. One run never
    reads both files, so sources are never mixed."""
    return YAHOO_SNAPSHOT_PATH if YAHOO_SNAPSHOT_PATH.exists() else SNAPSHOT_PATH


def agent_c_snapshot_paths() -> list[Path]:
    """Agent C's fallback files in order: the Yahoo one, then the Tiingo one, so a missing or unreadable Yahoo file
    (a bad deploy, a truncated copy) still leaves a snapshot of real prices. One run reads ONE of them."""
    return [p for p in (YAHOO_SNAPSHOT_PATH, SNAPSHOT_PATH) if p.exists()] or [SNAPSHOT_PATH]
FALLBACK_HISTORY_ROWS = 2520     # ~10 years, if AgentConfig has no goal_history_days


def default_history_rows() -> int:
    """Rows of history to keep: the most any live request asks Agent C for (the goal Monte Carlo's
    goal_history_days, or Agent A's lookback_days if longer). Read from AgentConfig, never changed."""
    from egoquant.models import AgentConfig
    cfg = AgentConfig()
    return max(int(getattr(cfg, "goal_history_days", FALLBACK_HISTORY_ROWS)), int(cfg.lookback_days))


def calendar_days(rows: int) -> int:
    """Calendar days Agent C fetches for `rows` trading days (MarketIntelligenceAgent.window_start).
    A request made on a later day starts later, so the snapshot still covers it."""
    return int(rows * 7 / 5) + 30


class SnapshotPriceSource:
    """Serves the committed snapshot. REAL prices (label "historical"), frozen at `prices_as_of`."""

    name = "snapshot"
    data_source: DataSourceLabel = "historical"

    def __init__(self, path: str | Path = SNAPSHOT_PATH):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
            self.prices_as_of = date.fromisoformat(payload["as_of"])
            self.sources: dict[str, str] = dict(payload.get("sources", {}))
            series = {t: pd.Series(p, dtype=float) for t, p in payload["prices"].items()}
        except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError, AttributeError) as exc:
            # EOFError / zlib.error: a truncated or damaged gzip file is not an OSError
            raise PriceSourceError(f"price snapshot unavailable ({type(exc).__name__})") from exc
        frame = pd.DataFrame(series)
        frame.index = pd.to_datetime(frame.index)
        self.prices = frame.sort_index()

    def origin_for(self, tickers=None) -> str:
        """WHO the prices come from, from the file's own per-ticker feed record (an old file without one: empty, no
        provider is named rather than a guessed one), e.g. "Yahoo Finance; crypto from Binance". With `tickers`, only the
        feeds of THOSE instruments are named, so a build with no crypto pair does not say "crypto from Binance"."""
        wanted = None if tickers is None else set(tickers)
        feeds = [f for t, f in self.sources.items() if wanted is None or t in wanted]
        funds = sorted({FEED_NAMES.get(f, f) for f in feeds if f != "binance"})
        crypto = "binance" in feeds
        return "; ".join(([" and ".join(funds)] if funds else []) + (["crypto from Binance"] if crypto else []))

    @property
    def origin(self) -> str:
        """origin_for the whole file."""
        return self.origin_for()

    def label_for(self, tickers=None) -> str:
        origin = self.origin_for(tickers)
        return f"real prices as of {self.prices_as_of.isoformat()}, cached" + (f" ({origin})" if origin else "")

    def calm_label_for(self, tickers=None) -> str:
        """The wording when the snapshot is the chosen source (EGOQUANT_PRICE_MODE=snapshot), not a fallback."""
        origin = self.origin_for(tickers)
        return f"saved snapshot of real prices as of {self.prices_as_of:%d %b %Y}" + (f", {origin}" if origin else "")

    @property
    def label(self) -> str:
        return self.label_for()

    @property
    def calm_label(self) -> str:
        return self.calm_label_for()

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        present = [t for t in tickers if t in self.prices.columns]
        frame = self.prices.loc[pd.Timestamp(start):pd.Timestamp(end), present].dropna(how="all")
        return frame.copy()


@functools.lru_cache(maxsize=4)
def load_snapshot(path: str = str(SNAPSHOT_PATH)) -> SnapshotPriceSource:
    """The snapshot, read once per process (read-only, so every Agent C can share it).
    Raises PriceSourceError if the file is missing or unreadable (that is not cached: tried again next time)."""
    return SnapshotPriceSource(path)


def snapshot_order(catalog: list[Instrument], include_stocks: bool = False) -> list[Instrument]:
    """What to fetch, most needed first: core funds (every portfolio needs them), crypto pairs (Binance,
    outside the Tiingo quota), the other funds, then single stocks only if asked (180 of them would use
    most of the shared monthly Tiingo ticker limit)."""
    core = [i for i in catalog if i.core]
    crypto = [i for i in catalog if not i.core and i.market == "crypto_spot"]
    funds = [i for i in catalog if not i.core and i.market != "crypto_spot" and i.product_type != "stock"]
    stocks = [i for i in catalog if not i.core and i.product_type == "stock"] if include_stocks else []
    return core + crypto + funds + stocks


def build_snapshot(as_of: date, catalog: list[Instrument] | None = None, path: str | Path = SNAPSHOT_PATH, *,
                   max_requests_per_hour: int = 20, include_stocks: bool = False, history_rows: int | None = None,
                   source: PriceSource | None = None, clock=time.monotonic, sleep=time.sleep) -> Path:
    """Fetch the catalog (live, through the disk cache, unless `source` is given) and write the file.

    Polite to the shared Tiingo quota: at most `max_requests_per_hour` NEW requests in any rolling hour
    (cache hits and Binance cost nothing), tickers refused by the monthly limit (TiingoUsage) are skipped,
    and the build sends NO new request after the first rate-limit error (tickers already on disk are
    still added). The file is rewritten before every wait and at the end, so a partial snapshot is
    usable at once; a rerun resumes from the disk cache.
    """
    catalog = catalog or DEFAULT_CATALOG
    history_rows = history_rows or default_history_rows()
    start = as_of - timedelta(days=calendar_days(history_rows))
    source = source or build_live_source({i.market for i in catalog}, catalog)
    order = snapshot_order(catalog, include_stocks)
    prices: dict[str, dict[str, float]] = {}
    feeds: dict[str, str] = {}
    skipped: dict[str, str] = {}
    sent: deque[float] = deque()                   # clock() of each new quota request in the last hour
    stopped: str | None = None                     # the rate-limit error that ended new requests

    def feed_of(ticker: str) -> Any:
        return source.source_for(ticker) if hasattr(source, "source_for") else source

    def write(stopped: str | None = None) -> None:
        missing = [i.ticker for i in order if i.ticker not in prices]
        payload = {"as_of": as_of.isoformat(), "start": start.isoformat(), "history_rows": history_rows,
                   "created_on": date.today().isoformat(),
                   "complete": not missing, "missing": missing, "skipped": skipped, "stopped": stopped,
                   "sources": feeds, "prices": prices}
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        tmp.replace(out)
        print(f"wrote {out} ({len(prices)} of {len(order)} tickers, as of {as_of})", flush=True)

    # Feeds that can download many tickers per request (Yahoo) get the whole list at once; the loop below
    # then serves those tickers from memory at no request cost.
    batches: dict[int, tuple[Any, list[str]]] = {}
    for inst in order:
        feed = feed_of(inst.ticker)
        if callable(getattr(feed, "prefetch", None)):
            batches.setdefault(id(feed), (feed, []))[1].append(inst.ticker)
    for feed, tickers in batches.values():
        try:
            feed.prefetch(tickers, start, as_of)
        except PriceSourceError as exc:            # what is missing is asked one ticker at a time below
            print(f"{feed.name}: batch download stopped ({exc}); the rest one ticker at a time", flush=True)

    for inst in order:
        feed = feed_of(inst.ticker)
        costs = inst.market == "us_listed" and bool(getattr(feed, "needs_request", lambda *a: False)(
            inst.ticker, start, as_of))
        if costs and stopped:
            continue
        if costs:
            if not getattr(feed, "allows_new", lambda t: True)(inst.ticker):
                skipped[inst.ticker] = "Tiingo monthly ticker limit"
                continue
            while sent and clock() - sent[0] >= 3600:
                sent.popleft()
            if len(sent) >= max_requests_per_hour:
                write()
                pause = max(0.0, 3600 - (clock() - sent[0]) + 5)    # the PC may have slept past the hour
                print(f"{max_requests_per_hour} requests this hour; waiting {pause / 60:.0f} min", flush=True)
                sleep(pause)
                while sent and clock() - sent[0] >= 3600:
                    sent.popleft()
            sent.append(clock())
        try:
            frame = source.fetch_adjusted_close([inst.ticker], start, as_of)
        except RateLimitError as exc:
            print(f"{inst.ticker}: {exc}; no more new requests, cached tickers only (rerun later to resume)",
                  flush=True)
            stopped = f"{inst.ticker}: {exc}"
            continue
        except PriceSourceError as exc:
            skipped[inst.ticker] = str(exc)
            print(f"{inst.ticker}: {exc}; left out", flush=True)
            continue
        if inst.ticker not in frame.columns or frame[inst.ticker].dropna().empty:
            skipped[inst.ticker] = "no data from the source"
            print(f"{inst.ticker}: no data, left out", flush=True)
            continue
        s = frame[inst.ticker].dropna()
        prices[inst.ticker] = {ts.strftime("%Y-%m-%d"): round(float(v), 6) for ts, v in s.items()}
        feeds[inst.ticker] = getattr(feed, "name", "none")
        print(f"{inst.ticker}: {len(s)} rows {s.index.min().date()} .. {s.index.max().date()}"
              f"{'' if costs else ' (cached)'}", flush=True)
    write(stopped)
    return Path(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the committed real-price snapshot")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--out", help="default: prices_snapshot.json.gz (tiingo) or prices_snapshot_yahoo.json.gz (yahoo)")
    parser.add_argument("--max-requests-per-hour", type=int, default=20)
    parser.add_argument("--include-stocks", action="store_true", help="also the catalog's single stocks")
    parser.add_argument("--years", type=float, help="history to keep (default: what the goal Monte Carlo asks for)")
    parser.add_argument("--source", choices=["tiingo", "yahoo"], default="tiingo",
                        help="feed for US-listed funds and stocks (yahoo: offline builds only, needs yfinance; "
                             "crypto pairs always come from Binance)")
    args = parser.parse_args()
    feed = None
    if args.source == "yahoo":
        from egoquant.agents.market_intel.yahoo import build_yahoo_source
        feed = build_yahoo_source(DEFAULT_CATALOG)
    out = args.out or str(YAHOO_SNAPSHOT_PATH if args.source == "yahoo" else SNAPSHOT_PATH)
    build_snapshot(args.as_of, path=out, max_requests_per_hour=args.max_requests_per_hour,
                   include_stocks=args.include_stocks, history_rows=round(args.years * 252) if args.years else None,
                   source=feed)
