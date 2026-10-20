"""Price sources for Agent C. Each returns ADJUSTED daily closes as a wide table.

    fetch_adjusted_close(tickers, start, end) -> DataFrame
        index   = tz-naive DatetimeIndex (trading days, ascending)
        columns = tickers that were found (a ticker with no data is simply missing; Agent C reports it)

Sources:
  * TiingoPriceSource     - REAL end-of-day adjusted prices for US-listed ETFs from the Tiingo REST API
                            (key from the TIINGO_API_KEY environment variable). Check Tiingo's
                            licence terms against the competition's "properly licensed data" rule.
  * BinancePriceSource    - REAL daily closes for crypto spot pairs from Binance's public market-data
                            API (data-api.binance.vision; no key needed).
  * CachedPriceSource     - keeps each ticker's fetched prices on disk, so repeated runs stay inside
                            Tiingo's free-tier limits (50 requests per hour).
  * TiingoUsage           - remembers the distinct tickers fetched from Tiingo this month, so Agent C
                            never runs into the free tier's 500-tickers-a-month limit mid-demo.
  * RoutedPriceSource     - sends each ticker to the right one of the above (build_live_source). This is
                            what Agent C uses by default.
  * CsvPriceSource        - a wide CSV of prices saved earlier (python -m egoquant fetch), for offline
                            demos when the internet or Binance is not reachable.
  * SyntheticPriceSource  - INVENTED prices, offline and deterministic; tests only (label "synthetic").

Sources only fetch. Cleaning, alignment and point-in-time cuts happen in Agent C (agent.py), so every
source is held to the same checks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import numpy as np
import pandas as pd
import requests

from egoquant import settings
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, Instrument, catalog_by_ticker

logger = logging.getLogger(__name__)
_WARNED_DISK: set[str] = set()


def _disk_unwritable(what: str, exc: OSError) -> None:
    """A cache that cannot be written (read-only disk, a path that is a file, a full volume) must cost speed, never
    the request: the caller keeps the data in memory. Logged once per kind, so a bad disk does not flood the log."""
    if what not in _WARNED_DISK:
        _WARNED_DISK.add(what)
        logger.warning("%s cannot be saved to the cache directory (%s: %s); keeping it in memory only", what,
                       type(exc).__name__, exc)

DataSourceLabel = Literal["synthetic", "historical"]


class PriceSource(Protocol):
    name: str
    data_source: DataSourceLabel

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame: ...


class PriceSourceError(RuntimeError):
    """The source could not be reached or refused the request (auth, rate limit, network)."""


_TIINGO_LOG: dict[str, deque] = {}   # key fingerprint -> monotonic times of this process's Tiingo requests
_TIINGO_LOG_LOCK = threading.Lock()


def key_fingerprint(api_key: str) -> str:
    """A short, non-reversible id for a key (logs and ledger file names never contain the key)."""
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()[:10]


def _log_tiingo_request(api_key: str) -> None:
    with _TIINGO_LOG_LOCK:
        _TIINGO_LOG.setdefault(key_fingerprint(api_key), deque()).append(time.monotonic())


def tiingo_requests_in_last(seconds: float = 3600.0, api_key: str | None = None) -> int:
    """Tiingo requests this process made in the last `seconds`, on one key or (None) on all keys."""
    cutoff = time.monotonic() - seconds
    with _TIINGO_LOG_LOCK:
        logs = [_TIINGO_LOG.get(key_fingerprint(api_key), deque())] if api_key else list(_TIINGO_LOG.values())
        for log in logs:
            while log and log[0] < cutoff:
                log.popleft()
        return sum(len(log) for log in logs)


class RateLimitError(PriceSourceError):
    """The source's request limit is reached. Agent C stops asking it and keeps what it already has."""


# After a 429 the key gets no request for this long, process-wide: every later request in the window would be
# refused too, and builds then switch to the snapshot at once instead of asking Tiingo again. PROVISIONAL.
TIINGO_COOLDOWN_SECONDS = 15 * 60
_TIINGO_COOLDOWN: dict[str, float] = {}         # key fingerprint -> monotonic time the cooldown ends
_TIINGO_LAST_429: dict[str, dict] = {}          # key fingerprint -> {"at": UTC time, "message": Tiingo's text}


def _note_tiingo_429(api_key: str, text: str) -> None:
    fp = key_fingerprint(api_key)
    with _TIINGO_LOG_LOCK:
        _TIINGO_COOLDOWN[fp] = time.monotonic() + TIINGO_COOLDOWN_SECONDS
        _TIINGO_LAST_429[fp] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "message": " ".join(str(text or "").replace(api_key, "<key>").split())[:200]}


def tiingo_cooldown_left(api_key: str) -> float:
    """Seconds until this key may ask Tiingo again after a 429 (0 = it may)."""
    with _TIINGO_LOG_LOCK:
        return max(0.0, _TIINGO_COOLDOWN.get(key_fingerprint(api_key), 0.0) - time.monotonic())


def tiingo_limit_status(api_key: str | None) -> dict:
    """For GET /data/status (public): whether a key is set, its last 429 and Tiingo's own message (which limit: hourly,
    daily or monthly), and the cooldown left. Never the key, and not even its fingerprint (a hash prefix would let
    anyone confirm a guessed key): only "set" / "not set"."""
    if not api_key:
        return {"key": "not set"}
    fp = key_fingerprint(api_key)
    with _TIINGO_LOG_LOCK:
        last = dict(_TIINGO_LAST_429.get(fp, {}))
    return {"key": "set", "last_429": last or None, "cooldown_seconds_left": round(tiingo_cooldown_left(api_key))}


CONNECT_TIMEOUT_NOTE = "default timeout = (connect 6 s, read 20 s): a blackholed network must fail in seconds"


def _get(session: Any, vendor: str, url: str, **kwargs: Any) -> Any:
    """session.get, with network failures (DNS, timeout, refused connection) as PriceSourceError,
    so Agent C's fallbacks see them like any other feed failure."""
    try:
        return session.get(url, **kwargs)
    except requests.RequestException as exc:
        raise PriceSourceError(f"{vendor} could not be reached (network error: {type(exc).__name__})") from exc


# --------------------------------------------------------------------------
# Synthetic (offline)
# --------------------------------------------------------------------------
class SyntheticPriceSource:
    """SYNTHETIC prices from a two-factor model (equity factor, rate factor) plus idiosyncratic noise.

    daily return_i = mu_i/252 + vol_i/sqrt(252) * (a_eq_i * f_eq + a_rate_i * f_rate + s_i * e_i)
    with s_i = sqrt(1 - a_eq_i^2 - a_rate_i^2), so each asset's volatility is exactly vol_i.

    The path is fixed on a calendar that starts at `origin`, so the same date always has the same
    price whatever window is requested (needed for point-in-time backtests). Each ticker's noise has
    its own seed, so adding an asset to the universe does not change the other assets' prices.
    All parameters are INVENTED; nothing here describes real markets.
    """

    name = "synthetic"
    data_source: DataSourceLabel = "synthetic"

    def __init__(self, seed: int = 7, catalog: list[Instrument] | None = None,
                 origin: date = date(2012, 1, 2)):
        self.seed = seed
        self.instruments = catalog_by_ticker(catalog or DEFAULT_CATALOG)
        self.origin = origin

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        dates = pd.bdate_range(start=pd.Timestamp(self.origin), end=pd.Timestamp(end))
        n = len(dates)
        factors = np.random.default_rng(self.seed).standard_normal((n, 2))
        columns: dict[str, np.ndarray] = {}
        for ticker in tickers:
            inst = self.instruments.get(ticker)
            if inst is None:
                continue  # unknown ticker -> missing column, like a real source
            a_eq, a_rate = inst.synthetic_equity_loading, inst.synthetic_rate_loading
            s2 = 1.0 - a_eq**2 - a_rate**2
            if s2 < 0:
                raise ValueError(f"{ticker}: synthetic loadings too large")
            noise = np.random.default_rng([self.seed, zlib.crc32(ticker.encode())]).standard_normal(n)
            z = a_eq * factors[:, 0] + a_rate * factors[:, 1] + np.sqrt(s2) * noise
            daily = inst.synthetic_mu / 252.0 + inst.synthetic_vol / np.sqrt(252.0) * z
            columns[ticker] = 100.0 * np.cumprod(1.0 + daily)
        frame = pd.DataFrame(columns, index=dates)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]


# --------------------------------------------------------------------------
# CSV (licensed data downloaded by the team)
# --------------------------------------------------------------------------
class CsvPriceSource:
    """Wide CSV: one 'date' column, then one column of ADJUSTED closes per ticker."""

    name = "csv"
    data_source: DataSourceLabel = "historical"

    def __init__(self, path: str | Path, date_column: str = "date", label: str | None = None):
        frame = pd.read_csv(path)
        if date_column not in frame.columns:
            raise ValueError(f"{path}: expected a {date_column!r} column")
        frame[date_column] = pd.to_datetime(frame[date_column], utc=True).dt.tz_localize(None).dt.normalize()
        self.prices = frame.set_index(date_column).sort_index().apply(pd.to_numeric, errors="coerce")
        if label:
            self.name = f"csv:{label}"

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        present = [t for t in tickers if t in self.prices.columns]
        return self.prices.loc[pd.Timestamp(start):pd.Timestamp(end), present].copy()


# --------------------------------------------------------------------------
# Tiingo (live)
# --------------------------------------------------------------------------
TIINGO_MONTHLY_TICKERS = 450        # PROVISIONAL: stay under the free tier's 500 distinct tickers a month


class TiingoUsage:
    """Distinct tickers fetched from Tiingo in the current month, kept in a small JSON file.

    New tickers are refused once `limit` is reached; tickers already used this month can always be
    fetched again (refreshing them does not count against the limit).
    """

    def __init__(self, path: str | Path | None = None, limit: int = TIINGO_MONTHLY_TICKERS,
                 today: Callable[[], date] = date.today, api_key: str | None = None):
        # One ledger per key: Tiingo counts distinct tickers per account.
        name = f"tiingo_usage_{key_fingerprint(api_key)}.json" if api_key else "tiingo_usage.json"
        self.path = Path(path) if path is not None else settings.cache_dir() / name
        self.limit = limit
        self.today = today
        self._memory: tuple[str, set[str]] = ("", set())      # (month, tickers): what this process used, if the disk fails

    def _load(self) -> set[str]:
        month = self.today().strftime("%Y-%m")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            saved = set(payload.get("tickers", [])) if payload.get("month") == month else set()
        except (OSError, ValueError):
            saved = set()
        return saved | (self._memory[1] if self._memory[0] == month else set())

    def allows(self, ticker: str) -> bool:
        used = self._load()
        return ticker.upper() in used or len(used) < self.limit

    def record(self, ticker: str) -> None:
        used = self._load()
        if ticker.upper() in used:
            return
        used.add(ticker.upper())
        month = self.today().strftime("%Y-%m")
        self._memory = (month, used)                    # the monthly limit holds for this process even if the disk fails
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"month": month, "tickers": sorted(used)}), encoding="utf-8")
        except OSError as exc:
            _disk_unwritable("The Tiingo usage ledger", exc)


class TiingoPriceSource:
    """End-of-day adjusted closes from https://api.tiingo.com/tiingo/daily/<ticker>/prices.

    `session` can be any object with a requests-style .get(); tests pass a fake one.
    """

    name = "tiingo"
    data_source: DataSourceLabel = "historical"
    BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"

    def __init__(self, api_key: str | None = None, session: Any | None = None, timeout: float | tuple[float, float] = (6.0, 20.0),
                 usage: TiingoUsage | None = None):
        self.api_key = api_key if api_key is not None else settings.tiingo_api_key()
        if not self.api_key:
            raise PriceSourceError(settings.TIINGO_KEY_HELP)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.usage = usage                          # None: no monthly ticker accounting (tests)

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        series: dict[str, pd.Series] = {}
        for ticker in tickers:
            left = tiingo_cooldown_left(self.api_key)
            if left > 0:                                # refused a moment ago: no request until the window passes
                raise RateLimitError(f"Tiingo rate limit reached (HTTP 429 earlier); no request for {left / 60:.0f} "
                                     f"more minute(s)")
            _log_tiingo_request(self.api_key)
            response = _get(
                self.session, "Tiingo", self.BASE_URL.format(ticker=ticker.lower()),
                params={"startDate": start.isoformat(), "endDate": end.isoformat(), "format": "json"},
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            if response.status_code == 404:
                continue  # unknown ticker -> missing column; Agent C reports it
            if response.status_code in (401, 403):
                raise PriceSourceError(f"Tiingo refused the API key (HTTP {response.status_code})")
            if response.status_code == 429:
                _note_tiingo_429(self.api_key, getattr(response, "text", ""))
                raise RateLimitError("Tiingo rate limit reached (HTTP 429); try again later")
            if response.status_code != 200:
                raise PriceSourceError(f"Tiingo error for {ticker}: HTTP {response.status_code}")
            if self.usage is not None:
                self.usage.record(ticker)
            rows = response.json()
            if not rows:
                continue
            index = pd.to_datetime([r["date"] for r in rows], utc=True).tz_localize(None).normalize()
            series[ticker] = pd.Series([r.get("adjClose") for r in rows], index=index, dtype=float)
        if not series:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        return pd.DataFrame(series).sort_index()

    def needs_request(self, ticker: str, start: date, end: date) -> bool:
        """Every ticker costs one request (Agent C's data budget counts these)."""
        return True

    def allows_new(self, ticker: str) -> bool:
        return self.usage is None or self.usage.allows(ticker)


# --------------------------------------------------------------------------
# Binance (live, crypto spot)
# --------------------------------------------------------------------------
class BinancePriceSource:
    """Daily closes from Binance spot klines (GET /api/v3/klines, interval 1d, UTC days).

    Uses the market-data-only host, which needs no API key. A daily candle is labeled with its UTC
    start date, and its close is the last trade before 00:00 UTC the next day. A candle that has not
    closed yet (today's) is never returned. Crypto spot prices need no dividend/split adjustment.
    Tickers are Binance symbols, e.g. "BTCUSDT".
    """

    name = "binance"
    data_source: DataSourceLabel = "historical"
    BASE_URL = "https://data-api.binance.vision/api/v3/klines"
    MAX_LIMIT = 1000

    def __init__(self, session: Any | None = None, timeout: float | tuple[float, float] = (6.0, 20.0), clock: Any = time.time):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock                          # injectable for tests

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        end_ms = int(end_dt.timestamp() * 1000) - 1
        now_ms = int(self.clock() * 1000)
        series: dict[str, pd.Series] = {}
        for ticker in tickers:
            closes: dict[pd.Timestamp, float] = {}
            cursor = start_ms
            while cursor <= end_ms:
                response = _get(
                    self.session, "Binance", self.BASE_URL,
                    params={"symbol": ticker.upper(), "interval": "1d", "startTime": cursor,
                            "endTime": end_ms, "limit": self.MAX_LIMIT},
                    timeout=self.timeout,
                )
                if response.status_code == 400:
                    break  # unknown symbol (Binance error -1121) -> missing column; Agent C reports it
                if response.status_code in (418, 429):
                    raise RateLimitError(f"Binance rate limit reached (HTTP {response.status_code})")
                if response.status_code == 451:
                    raise PriceSourceError("Binance refused the request from this location (HTTP 451)")
                if response.status_code != 200:
                    raise PriceSourceError(f"Binance error for {ticker}: HTTP {response.status_code}")
                rows = response.json()
                if not rows:
                    break
                for row in rows:
                    open_ms, close_price, close_ms = int(row[0]), float(row[4]), int(row[6])
                    if close_ms <= now_ms:  # skip the candle that is still open
                        day = pd.Timestamp(open_ms, unit="ms").normalize()
                        closes[day] = close_price
                cursor = int(rows[-1][0]) + 86_400_000
                if len(rows) < self.MAX_LIMIT:
                    break
            if closes:
                series[ticker] = pd.Series(closes, dtype=float)
        if not series:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        return pd.DataFrame(series).sort_index()

    def needs_request(self, ticker: str, start: date, end: date) -> bool:
        return True


class BinanceMarket:
    """Every coin tradable on Binance spot against USDT, ranked by 24-hour trading volume (in USDT).

    Two public requests (exchangeInfo, weight 20; 24h tickers, weight 80) out of Binance's ~6,000 per
    minute. Left out, because they are not crypto-assets:
      * stablecoins and fiat currencies (cash-like);
      * gold-backed tokens (Agent C's catalog handles them as gold);
      * TOKENIZED SECURITIES: Binance lists shares and ETFs as tokens named <ticker>B (AAPLB, NVDAB,
        SPYB, and leveraged ETFs such as TQQQB). Every base asset ending in "B" is treated as one,
        except the crypto-assets in REAL_COINS_ENDING_IN_B (checked by hand on 2026-10-18). That fails
        safe: a new tokenized share is excluded automatically; a new coin ending in "B" is excluded
        until it is added to the list.
    """

    INFO_URL = "https://data-api.binance.vision/api/v3/exchangeInfo"
    TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"
    QUOTE = "USDT"
    NOT_CRYPTO_ASSETS = frozenset({
        "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "PYUSD", "RLUSD", "EURI", "AEUR", "XUSD",
        "EUR", "GBP", "TRY", "BRL", "ARS", "JPY", "MXN", "PLN", "RON", "UAH", "ZAR", "COP", "CZK", "IDR",
        "PAXG", "XAUT",
    })
    REAL_COINS_ENDING_IN_B = frozenset({"BNB", "ARB", "BB", "CKB", "DGB", "SHIB", "TRB", "YB"})

    @classmethod
    def is_crypto_asset(cls, base: str) -> bool:
        if base in cls.NOT_CRYPTO_ASSETS:
            return False
        return not base.endswith("B") or base in cls.REAL_COINS_ENDING_IN_B

    def __init__(self, session: Any | None = None, timeout: float | tuple[float, float] = (6.0, 20.0)):
        self.session = session or requests.Session()
        self.timeout = timeout

    def ranked_pairs(self) -> list[tuple[str, str, float]]:
        """(symbol, base asset, 24h quote volume in USDT), highest volume first."""
        info = self._get(self.INFO_URL, {"permissions": "SPOT"})
        tickers = self._get(self.TICKER_URL, {"type": "MINI"})
        volume = {t["symbol"]: float(t.get("quoteVolume") or 0.0) for t in tickers}
        pairs = [(s["symbol"], s["baseAsset"], volume.get(s["symbol"], 0.0)) for s in info["symbols"]
                 if s.get("status") == "TRADING" and s.get("quoteAsset") == self.QUOTE
                 and s.get("isSpotTradingAllowed", True) and self.is_crypto_asset(s["baseAsset"])]
        return sorted(pairs, key=lambda p: (-p[2], p[0]))

    def _get(self, url: str, params: dict) -> Any:
        response = _get(self.session, "Binance", url, params=params, timeout=self.timeout)
        if response.status_code in (418, 429):
            raise PriceSourceError(f"Binance rate limit reached (HTTP {response.status_code})")
        if response.status_code == 451:
            raise PriceSourceError("Binance refused the request from this location (HTTP 451)")
        if response.status_code != 200:
            raise PriceSourceError(f"Binance error: HTTP {response.status_code}")
        return response.json()


# --------------------------------------------------------------------------
# Disk cache (wraps a live source)
# --------------------------------------------------------------------------
class CachedPriceSource:
    """Serves a ticker from disk when an earlier fetch fully covers the requested range.

    One JSON file per ticker: the range that was fetched, the day it was fetched and the prices.
    A cached range is trusted only up to the day BEFORE it was fetched, because today's bar may not
    be final yet. Misses are fetched and saved one ticker at a time, so progress survives a rate limit. A miss refetches the whole requested range and REPLACES the file: adjusted closes
    are restated after every dividend or split, so old and new fetches are never stitched together.
    Tickers the source does not know are not cached (they are asked again next time).

    Every file read or written is also kept in memory (shared by all instances), so the API serves repeated
    requests without touching the disk or the vendor. At most settings.price_memory_cache_files() files are kept;
    the least recently used is dropped first (it is read from disk again when needed), so a long-running server
    does not grow without limit.
    """

    _memory: OrderedDict[Path, dict] = OrderedDict()     # cache file -> its payload, across requests (LRU)
    _memory_lock = threading.Lock()                      # requests run in a thread pool
    _disk_lock = threading.Lock()                        # one reader/writer of the cache files at a time (Windows
                                                         # refuses a rename onto a file another thread has open)

    @classmethod
    def _recall(cls, path: Path) -> dict | None:
        with cls._memory_lock:
            payload = cls._memory.get(path)
            if payload is not None:
                cls._memory.move_to_end(path)
            return payload

    @staticmethod
    def _compact(payload: dict) -> dict:
        """The payload with its prices as two numpy arrays (dates, values): ~12 bytes a day instead of ~100 for a dict
        of text dates, so hundreds of 20-year histories fit in memory (portfolio method v2 prices ~290 products at once).
        Same numbers, same order; a payload that does not parse stays as it is."""
        prices = payload.get("prices")
        if not isinstance(prices, dict):
            return payload
        try:
            keys = sorted(prices)
            index = pd.to_datetime(keys).to_numpy()          # the same parsing as the disk path, so the same dtype
            values = np.array([prices[k] for k in keys], dtype=float)
        except (ValueError, TypeError):
            return payload
        return {**{k: v for k, v in payload.items() if k != "prices"}, "index": index, "values": values}

    @classmethod
    def _remember(cls, path: Path, payload: dict) -> None:
        payload = cls._compact(payload)
        with cls._memory_lock:
            cls._memory[path] = payload
            cls._memory.move_to_end(path)
            limit = settings.price_memory_cache_files()
            while len(cls._memory) > limit:
                cls._memory.popitem(last=False)

    def __init__(self, inner: Any, directory: str | Path | None = None,
                 today: Callable[[], date] = date.today):
        self.inner = inner
        self.name = inner.name
        self.data_source: DataSourceLabel = inner.data_source
        self.directory = Path(directory) if directory is not None else settings.cache_dir() / inner.name
        self.today = today

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        series: dict[str, pd.Series] = {}
        missing: list[str] = []
        for ticker in tickers:
            cached = self._load(ticker, start, end)
            if cached is None:
                missing.append(ticker)
            else:
                series[ticker] = cached
        # One ticker per call (the live sources request one ticker at a time anyway), saved as soon as
        # it arrives: if a rate limit stops the run halfway, the next run starts where this one stopped.
        for ticker in missing:
            fresh = self.inner.fetch_adjusted_close([ticker], start, end)
            if ticker in fresh.columns and fresh[ticker].notna().any():
                series[ticker] = fresh[ticker].dropna()
                self._save(ticker, start, end, series[ticker])
        if not series:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        return pd.DataFrame(series).sort_index()

    def allows_new(self, ticker: str) -> bool:
        check = getattr(self.inner, "allows_new", None)
        return bool(check(ticker)) if callable(check) else True

    def needs_request(self, ticker: str, start: date, end: date) -> bool:
        """False when the disk already covers the range (a cache hit costs no request)."""
        if self._load(ticker, start, end) is not None:
            return False
        check = getattr(self.inner, "needs_request", None)
        return bool(check(ticker, start, end)) if callable(check) else False

    def _path(self, ticker: str) -> Path:
        return self.directory / f"{ticker.upper()}.json"

    def _load(self, ticker: str, start: date, end: date) -> pd.Series | None:
        path = self._path(ticker)
        payload = self._recall(path)
        if payload is None:
            try:
                with self._disk_lock:
                    if not path.exists():
                        return None
                    text = path.read_text(encoding="utf-8")
                payload = json.loads(text)
            except (OSError, ValueError):
                return None                              # unreadable cache file (damaged, or no permission): fetch again
        try:
            first = date.fromisoformat(payload["start"])
            last = min(date.fromisoformat(payload["end"]),
                       date.fromisoformat(payload["fetched_on"]) - timedelta(days=1))
            prices = payload["values"] if "values" in payload else payload["prices"]
        except (ValueError, KeyError, TypeError):
            return None                                  # unreadable cache file: fetch again
        if not (first <= start and end <= last):
            return None
        self._remember(path, payload)
        if "values" in payload:                          # remembered in memory (compact arrays)
            s = pd.Series(prices, index=pd.DatetimeIndex(payload["index"]), dtype=float)
        else:
            s = pd.Series(prices, dtype=float)
            s.index = pd.to_datetime(s.index)
        return s.sort_index().loc[pd.Timestamp(start):pd.Timestamp(end)]

    def _save(self, ticker: str, start: date, end: date, prices: pd.Series) -> None:
        payload = {"ticker": ticker, "source": self.name, "start": start.isoformat(), "end": end.isoformat(),
                   "fetched_on": self.today().isoformat(),
                   "prices": {ts.strftime("%Y-%m-%d"): float(v) for ts, v in prices.items()}}
        # one temporary file per writer: two builds saving the same ticker at once (thread pool) must not share it
        tmp = self._path(ticker).with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        text = json.dumps(payload)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._disk_lock:
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self._path(ticker))
        except OSError as exc:                           # the price is still good: serve it from memory
            _disk_unwritable("Fetched prices", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._remember(self._path(ticker), payload)


# --------------------------------------------------------------------------
# Routing: each ticker to the source for its market
# --------------------------------------------------------------------------
OPTIONAL_MARKETS = frozenset({"crypto_spot"})   # a failure here drops those assets; the rest stays real


class RoutedPriceSource:
    """Combines one source per market ("us_listed", "crypto_spot") into a single PriceSource.

    If the feed of an OPTIONAL market fails (e.g. Binance answers HTTP 451 to servers in the US), its
    tickers come back missing and the reason is kept in `failures` (market -> reason), so Agent C drops
    only those assets, with that reason, and keeps the real prices of every other market.
    """

    def __init__(self, sources: dict[str, Any], catalog: list[Instrument] | None = None,
                 optional_markets: frozenset[str] = OPTIONAL_MARKETS):
        self.sources = sources
        self.optional_markets = optional_markets
        self.failures: dict[str, str] = {}
        self.instruments = catalog_by_ticker(catalog or DEFAULT_CATALOG)
        names = sorted({s.name for s in sources.values()})
        self.name = "+".join(names)
        labels = {s.data_source for s in sources.values()}
        self.data_source: DataSourceLabel = "synthetic" if "synthetic" in labels else "historical"

    def source_for(self, ticker: str) -> Any | None:
        inst = self.instruments.get(ticker)
        return None if inst is None else self.sources.get(inst.market)

    def allows_new(self, ticker: str) -> bool:
        check = getattr(self.source_for(ticker), "allows_new", None)
        return bool(check(ticker)) if callable(check) else True

    def needs_request(self, ticker: str, start: date, end: date) -> bool:
        """Whether pricing `ticker` costs a request at its market's source (offline sources: never)."""
        check = getattr(self.source_for(ticker), "needs_request", None)
        return bool(check(ticker, start, end)) if callable(check) else False

    def fetch_adjusted_close(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        groups: dict[int, tuple[Any, str, list[str]]] = {}
        for ticker in tickers:
            source = self.source_for(ticker)
            if source is not None:  # no source for this market -> missing column, reported by Agent C
                groups.setdefault(id(source), (source, self.instruments[ticker].market, []))[2].append(ticker)
        frames = []
        for source, market, group in groups.values():
            if market in self.failures:
                continue                                 # failed earlier in this run: do not ask again
            try:
                frames.append(source.fetch_adjusted_close(group, start, end))
            except RateLimitError:
                raise                                    # temporary: Agent C stops asking, keeps what it has
            except PriceSourceError as exc:
                if market not in self.optional_markets:
                    raise
                self.failures[market] = str(exc)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(index=pd.DatetimeIndex([]))
        return pd.concat(frames, axis=1, sort=True)


def build_live_source(markets: set[str], catalog: list[Instrument] | None = None,
                      tiingo_api_key: str | None = None, cache: bool = True) -> RoutedPriceSource:
    """Real data for exactly the markets this investor needs, cached on disk unless cache=False.

    Raises PriceSourceError with a fix if a feed cannot be set up.
    """
    sources: dict[str, Any] = {}
    if "us_listed" in markets:
        try:
            key = tiingo_api_key if tiingo_api_key is not None else settings.tiingo_api_key()   # the traders' key
            sources["us_listed"] = TiingoPriceSource(api_key=key, usage=TiingoUsage(api_key=key or None))
        except PriceSourceError as exc:
            raise PriceSourceError(
                f"{exc}. Real ETF prices need a Tiingo key (free at tiingo.com). For an offline run "
                "pass source=CsvPriceSource(...) or source=SyntheticPriceSource() (invented data)."
            ) from exc
    if "crypto_spot" in markets:
        sources["crypto_spot"] = BinancePriceSource()
    if cache:
        sources = {market: CachedPriceSource(source) for market, source in sources.items()}
    return RoutedPriceSource(sources, catalog)
