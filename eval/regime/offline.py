"""Offline harness for the regime eval: EgoQuant's REAL production wiring on a COPY of a frozen price cache, no network.

Adapted from ZC's eval/zc/offline.py (Oct 18). install() must run BEFORE egoquant is imported:
  * the frozen cache (REGIME_FROZEN_CACHE, default the ZC copy dated 2026-10-18) is copied to a temporary folder and
    EGOQUANT_CACHE_DIR points there, so nothing can be written to the shared cache;
  * Tiingo/OpenAI keys blank, briefing, cache warmer and news worker off, price snapshot off, no synthetic fallback;
  * every socket connect and DNS lookup outside loopback raises: no code path can reach Tiingo, Binance or OpenAI;
  * prices: Agent C's live source is replaced by the same cache stack whose inner "vendor" answers every cache miss
    with "no data" (Agent C then uses the next fund of the same cluster, as for a ticker Tiingo does not have);
  * the investment directory is read once from the frozen copy; Binance coins are ranked by a stand-in (longest
    cached history first), because real 24h volumes are not cached.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN_CACHE = Path(os.environ.get("REGIME_FROZEN_CACHE", r"C:\Users\ASUS\zc-data\cache-frozen-20261018"))
AS_OF = date(2026, 10, 16)                       # last close in the frozen cache
OFFLINE_MISSES: list[str] = []
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_STATE: dict = {}


def _blocked(*_a, **_k):
    raise OSError("regime eval offline guard: network access is disabled")


def _guard(original):
    def connect(self, address, *a, **k):
        host = str(address[0]) if isinstance(address, tuple) and address else str(address)
        if host not in _LOOPBACK:
            _blocked()
        return original(self, address, *a, **k)
    return connect


def _guard_dns(original):
    def getaddrinfo(host, *a, **k):
        if host is not None and host not in _LOOPBACK:
            _blocked()
        return original(host, *a, **k)
    return getaddrinfo


def install(regime_flag: str | None = None) -> Path:
    """Environment, network guard, offline sources. Returns the private cache copy. Call before importing egoquant."""
    if _STATE.get("cache"):
        return _STATE["cache"]
    if "egoquant" in sys.modules:
        raise RuntimeError("offline.install() must run before egoquant is imported")
    if not FROZEN_CACHE.is_dir():
        raise RuntimeError(f"frozen cache not found: {FROZEN_CACHE} (set REGIME_FROZEN_CACHE)")
    cache = Path(tempfile.mkdtemp(prefix="regime-eval-cache-"))
    for sub in ("tiingo", "binance"):
        shutil.copytree(FROZEN_CACHE / sub, cache / sub)
    shutil.copy2(FROZEN_CACHE / "directory.json", cache / "directory.json")
    os.environ.update({
        "EGOQUANT_CACHE_DIR": str(cache), "TIINGO_API_KEY": "", "TIINGO_WARM_API_KEY": "", "OPENAI_API_KEY": "",
        "EGOQUANT_BRIEFING": "off", "EGOQUANT_BRIEFING_LLM": "0", "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off",
        "EGOQUANT_WARM": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_PRICE_SNAPSHOT": "off", "EGOQUANT_DATA": "live",
        "EGOQUANT_CONTEXT_SECRET": "regime-eval-secret",
    })
    os.environ.pop("EGOQUANT_DATA_FALLBACK", None)
    if regime_flag is not None:
        os.environ["EGOQUANT_REGIME_MC"] = regime_flag
    sys.path.insert(0, str(ROOT))
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False                     # type: ignore[assignment]
    socket.socket.connect = _guard(socket.socket.connect)          # type: ignore[assignment]
    socket.socket.connect_ex = _guard(socket.socket.connect_ex)    # type: ignore[assignment]
    socket.getaddrinfo = _guard_dns(socket.getaddrinfo)            # type: ignore[assignment]

    import pandas as pd

    from egoquant.agents.market_intel import agent as agent_mod
    from egoquant.agents.market_intel import directory as directory_mod
    from egoquant.agents.market_intel import snapshot as snapshot_mod
    from egoquant.agents.market_intel import sources as sources_mod

    frozen = {sub: {f.stem: f for f in (FROZEN_CACHE / sub).glob("*.json")} for sub in ("tiingo", "binance")}

    class OfflineVendor:
        """Inner source of the cache. A cache miss because the requested RANGE is longer than the cached one (e.g. VGT
        was cached from 2024 only) gets whatever the frozen cache holds for that ticker, the same data the portfolio
        build used (a real vendor would send the longer history). A ticker that is not cached at all gets no data."""

        def __init__(self, name: str):
            self.name, self.data_source = name, "historical"

        def fetch_adjusted_close(self, tickers, start, end):
            series = {}
            for t in tickers:
                path = frozen.get(self.name, {}).get(t.upper())
                if path is None:
                    OFFLINE_MISSES.append(t)
                    continue
                s = pd.Series(json.loads(path.read_text(encoding="utf-8"))["prices"], dtype=float)
                s.index = pd.to_datetime(s.index)
                s = s.sort_index().loc[pd.Timestamp(start):pd.Timestamp(end)]
                if len(s):
                    series[t] = s
            return pd.DataFrame(series) if series else pd.DataFrame(index=pd.DatetimeIndex([]))

        def needs_request(self, ticker, start, end):
            return True

        def allows_new(self, ticker):
            return True

    def offline_build(markets, catalog=None, tiingo_api_key=None, cache=True):
        sources = {}
        if "us_listed" in markets:
            sources["us_listed"] = sources_mod.CachedPriceSource(OfflineVendor("tiingo"))
        if "crypto_spot" in markets:
            sources["crypto_spot"] = sources_mod.CachedPriceSource(OfflineVendor("binance"))
        return sources_mod.RoutedPriceSource(sources, catalog)

    for mod in (agent_mod, sources_mod, snapshot_mod):
        mod.build_live_source = offline_build

    coins = []
    for f in sorted((cache / "binance").glob("*USDT.json")):
        try:
            n = len(json.loads(f.read_text(encoding="utf-8"))["prices"])
        except (ValueError, KeyError):
            continue
        coins.append((f.stem, f.stem[:-4], float(n)))
    coins.sort(key=lambda c: (-c[2], c[0]))
    sources_mod.BinanceMarket.ranked_pairs = lambda self: [c for c in coins if sources_mod.BinanceMarket.is_crypto_asset(c[1])]

    saved = directory_mod.Directory.model_validate_json((cache / "directory.json").read_text(encoding="utf-8"))
    directory_mod.load_directory = lambda *_a, **_k: saved
    agent_mod.load_directory = lambda *_a, **_k: saved
    _STATE["cache"] = cache
    return cache


def install_routes():
    """For runs through the HTTP layer: pinned decision date, directory loaded, no rate limit."""
    install()
    import egoquant_routes as routes
    from egoquant.agents.market_intel import directory as directory_mod
    directory = directory_mod.load_directory()
    routes._last_weekday_before = lambda day: AS_OF
    routes.load_directory = lambda *a, **k: directory
    with routes._directory_lock:
        routes._directory, routes._directory_checked_on = directory, date.today()
    routes._check_portfolio_rate = lambda *a, **k: None
    return routes


def cleanup() -> None:
    cache = _STATE.pop("cache", None)
    if cache:
        shutil.rmtree(cache, ignore_errors=True)


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def process_memory_mb() -> tuple[float, float]:
    """(current, peak) working set of this process in MB (Windows; RSS-like). (nan, nan) elsewhere."""
    if os.name != "nt":
        return float("nan"), float("nan")
    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return pmc.WorkingSetSize / 2**20, pmc.PeakWorkingSetSize / 2**20
