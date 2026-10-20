"""Yahoo Finance as an OFFLINE source for the committed price snapshot (never used by live portfolio builds).

    python -m egoquant.agents.market_intel.snapshot --as-of 2026-10-16 --source yahoo [--include-stocks]

Why: Tiingo's free tier serves one ticker per request (50 an hour, 500 distinct tickers a month), so the
snapshot of ~110 funds took a night and the catalog's 180 single stocks never fit. Yahoo returns many tickers
in one request, so the whole catalog takes a few minutes.

Rules:
  * yfinance is NOT in requirements.txt: the deployed app never imports it and never calls Yahoo. Install it
    on the machine that builds the snapshot (pip install yfinance).
  * yfinance reads Yahoo's unofficial, undocumented endpoints. Check Yahoo's terms against the competition's
    "properly licensed data" rule before committing a snapshot built from it.
  * Each ticker's feed is recorded in the snapshot ("sources": {"VT": "yahoo"}), so the prices stay labeled.
    Crypto pairs still come from Binance; the snapshot never mixes feeds within one ticker.
  * Prices are adjusted closes (auto_adjust: splits and dividends), like Tiingo's adjClose.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from egoquant.agents.market_intel.catalog import Instrument
from egoquant.agents.market_intel.sources import (
    BinancePriceSource,
    CachedPriceSource,
    DataSourceLabel,
    PriceSourceError,
    RateLimitError,
    RoutedPriceSource,
)

YAHOO_CHUNK = 100                   # PROVISIONAL: tickers per download request

# (tickers, start, end inclusive) -> (adjusted closes, {ticker: error text} for the tickers that failed)
Downloader = Callable[[list[str], date, date], tuple[pd.DataFrame, dict[str, str]]]


def _ascii_ca_bundle() -> str:
    """certifi's CA file at a path curl can open. curl (inside yfinance) cannot read a CA file whose path has
    non-ASCII characters (e.g. a venv under 'EgoQuantAsıl'), and then every request fails with an SSL error."""
    import certifi
    path = certifi.where()
    if path.isascii():
        return path
    copy = os.path.join(tempfile.gettempdir(), "egoquant_cacert.pem")
    if not copy.isascii():
        raise PriceSourceError("Yahoo: no ASCII path for the CA certificates; set TMP to an ASCII folder")
    shutil.copyfile(path, copy)
    return copy


def yfinance_download(tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, dict[str, str]]:
    """One yfinance request for `tickers`: adjusted daily closes from `start` to `end` (both inclusive)."""
    try:
        import yfinance as yf
        import yfinance.shared as yf_shared
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise PriceSourceError("Yahoo needs yfinance on the machine that builds the snapshot "
                               "(pip install yfinance; it is not a dependency of the app)") from exc
    session = curl_requests.Session(impersonate="chrome", verify=_ascii_ca_bundle())
    frame = yf.download(tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                        auto_adjust=True, actions=False, progress=False, threads=True, session=session,
                        group_by="column", multi_level_index=True)
    errors = {str(t): str(e) for t, e in dict(getattr(yf_shared, "_ERRORS", {})).items()}
    if frame is None or frame.empty or "Close" not in frame.columns.get_level_values(0):
        return pd.DataFrame(index=pd.DatetimeIndex([])), errors
    return frame["Close"], errors


def _is_rate_limit(text: str) -> bool:
    return any(k in text for k in ("Too Many Requests", "Rate limit", "RateLimit", "429"))


def _is_network(text: str) -> bool:
    return any(k in text for k in ("SSLError", "curl:", "Connection", "Timeout", "timed out", "resolve host"))


class YahooPriceSource:
    """Adjusted daily closes from Yahoo Finance, downloaded in batches of `chunk_size` tickers.

    `prefetch` downloads a whole list at once and keeps it in memory; `fetch_adjusted_close` serves from
    memory and downloads only what was not prefetched. A ticker Yahoo does not know is a missing column
    (and is not asked again in this run), like every other source. `download` is injectable; tests pass a fake.
    """

    name = "yahoo"
    data_source: DataSourceLabel = "historical"

    def __init__(self, download: Downloader | None = None, chunk_size: int = YAHOO_CHUNK):
        self.download = download or yfinance_download
        self.chunk_size = chunk_size
        self._memo: dict[str, pd.Series | None] = {}        # ticker -> closes (None: Yahoo has no data)
        self._range: tuple[date, date] | None = None         # the range the memo holds

    def prefetch(self, tickers: list[str], start: date, end: date) -> None:
        """Download every ticker not yet in memory, `chunk_size` per request."""
        if self._range != (start, end):
            self._memo, self._range = {}, (start, end)
        todo = [t for t in dict.fromkeys(tickers) if t not in self._memo]
        for i in range(0, len(todo), self.chunk_size):
            chunk = todo[i:i + self.chunk_size]
            frame, errors = self.download(chunk, start, end)
            got = {t: frame[t].dropna() for t in chunk if t in frame.columns and frame[t].notna().any()}
            failed = [t for t in chunk if t not in got]
            reasons = [errors.get(t, "") for t in failed]
            if failed and any(_is_rate_limit(r) for r in reasons):
                for t, s in got.items():
                    self._memo[t] = self._clean(s)
                raise RateLimitError("Yahoo rate limit reached (Too Many Requests); try again later")
            if not got and reasons and all(_is_network(r) for r in reasons):
                raise PriceSourceError(f"Yahoo could not be reached ({reasons[0][:120]})")
            for t in chunk:
                self._memo[t] = self._clean(got[t]) if t in got else None

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        self.prefetch(tickers, start, end)
        series = {t: self._memo[t] for t in tickers if self._memo.get(t) is not None}
        if not series:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        return pd.DataFrame(series).sort_index()

    def needs_request(self, ticker: str, start: date, end: date) -> bool:
        """False once the ticker is in memory for this range (prefetched, or known to have no data)."""
        return self._range != (start, end) or ticker not in self._memo

    @staticmethod
    def _clean(s: pd.Series) -> pd.Series:
        index = pd.DatetimeIndex(s.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        out = pd.Series(s.to_numpy(dtype=float), index=index.normalize().astype("datetime64[ns]"))
        return out[~out.index.duplicated(keep="last")].sort_index()


def build_yahoo_source(catalog: list[Instrument] | None = None, download: Downloader | None = None) -> RoutedPriceSource:
    """For the snapshot builder: Yahoo for US-listed funds and stocks, Binance (disk-cached) for crypto pairs."""
    return RoutedPriceSource({"us_listed": YahooPriceSource(download=download),
                              "crypto_spot": CachedPriceSource(BinancePriceSource())}, catalog)
