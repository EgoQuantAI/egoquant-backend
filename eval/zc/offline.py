"""ZC offline harness: run EgoQuant's REAL production wiring on a FROZEN copy of the price cache, with no network.

Rules (Onur, Oct 18): cached prices only, no new Tiingo requests, no OpenAI, nothing written to the shared folder
or the shared cache. install() enforces them; it must run BEFORE egoquant is imported.

  * Environment: a private frozen cache (EGOQUANT_CACHE_DIR), Tiingo/OpenAI keys blank, briefing and cache warmer
    off, price snapshot off and no synthetic fallback (a missing price fails loudly, never silently replaced).
  * Hard guard: every socket connect / DNS lookup raises, so no code path can reach Tiingo, Binance, Nasdaq or OpenAI.
  * Prices: build_live_source is swapped for the same CachedPriceSource + RoutedPriceSource stack, whose inner
    "vendor" answers every cache MISS with "no data" (Agent C then treats the ticker as unpriced and uses the next one
    of the same cluster, exactly as for a ticker Tiingo does not have). Misses are counted (OFFLINE_MISSES).
  * Directory: loaded read-only from the frozen file (never rebuilt).
  * Binance coin list: real 24h volumes are not cached, so the cached coins are listed with a STAND-IN order
    (longest cached history first, then symbol). Same count of coins as the cache holds; which coins rank first
    can differ from production. Labelled in every result.
  * Decision date pinned to AS_OF (the last cached close), so results do not depend on the day they are run.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                     # the ZC worktree
FROZEN_CACHE = Path(os.environ.get("ZC_FROZEN_CACHE", r"C:\Users\ASUS\zc-data\cache-frozen-20261018"))
AS_OF = date(2026, 10, 16)                                     # last close in the frozen cache (fetched 2026-10-18)
BINANCE_ORDER_NOTE = ("Binance coin list offline: cached coins ranked by a stand-in (longest cached history first), "
                      "not by real 24h volume")
OFFLINE_MISSES: list[str] = []
_INSTALLED = False


def _blocked(*_args, **_kwargs):
    raise OSError("ZC offline guard: network access is disabled in this run")


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host(address) -> str:
    return str(address[0]) if isinstance(address, tuple) and address else str(address)


def _guard(original):
    """Allow loopback only (asyncio's internal socket pair on Windows); anything else raises."""
    def connect(self, address, *a, **k):
        if _host(address) not in _LOOPBACK:
            _blocked()
        return original(self, address, *a, **k)
    return connect


def _guard_dns(original):
    def getaddrinfo(host, *a, **k):
        if host not in _LOOPBACK and host is not None:
            _blocked()
        return original(host, *a, **k)
    return getaddrinfo


def install() -> None:
    """Environment, network guard and offline sources. Idempotent; call before importing egoquant."""
    global _INSTALLED
    if _INSTALLED:
        return
    if "egoquant" in sys.modules:
        raise RuntimeError("offline.install() must run before egoquant is imported")
    if not FROZEN_CACHE.is_dir():
        raise RuntimeError(f"frozen cache not found: {FROZEN_CACHE}")
    os.environ.update({
        "EGOQUANT_CACHE_DIR": str(FROZEN_CACHE), "TIINGO_API_KEY": "", "TIINGO_WARM_API_KEY": "", "OPENAI_API_KEY": "",
        "EGOQUANT_BRIEFING": "off", "EGOQUANT_BRIEFING_LLM": "0", "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off",
        "EGOQUANT_WARM": "off", "EGOQUANT_PRICE_SNAPSHOT": "off", "EGOQUANT_DATA": "live",
    })
    os.environ.pop("EGOQUANT_DATA_FALLBACK", None)
    sys.path.insert(0, str(ROOT))
    # dotenv must not bring the real keys back: main.py calls load_dotenv(.env) (the worktree has no .env, but
    # make sure even if one appears)
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

    class OfflineVendor:
        """Inner source of the cache: every miss gets 'no data' (and is counted), never a request."""

        def __init__(self, name: str):
            self.name, self.data_source = name, "historical"

        def fetch_adjusted_close(self, tickers, start, end):
            OFFLINE_MISSES.extend(tickers)
            return pd.DataFrame(index=pd.DatetimeIndex([]))

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
    for f in sorted((FROZEN_CACHE / "binance").glob("*USDT.json")):
        try:
            n = len(json.loads(f.read_text(encoding="utf-8"))["prices"])
        except (ValueError, KeyError):
            continue
        coins.append((f.stem, f.stem[:-4], float(n)))             # stand-in "volume": cached days
    coins.sort(key=lambda c: (-c[2], c[0]))
    sources_mod.BinanceMarket.ranked_pairs = lambda self: [c for c in coins if sources_mod.BinanceMarket.is_crypto_asset(c[1])]

    saved = directory_mod.Directory.model_validate_json((FROZEN_CACHE / "directory.json").read_text(encoding="utf-8"))

    def read_only_directory(*_a, **_k):
        return saved

    directory_mod.load_directory = read_only_directory
    agent_mod.load_directory = read_only_directory
    _INSTALLED = True


def install_routes():
    """For runs through the HTTP layer: pinned decision date, directory already loaded, no rate limit."""
    install()
    import egoquant_routes as routes
    routes._last_weekday_before = lambda day: AS_OF
    routes.load_directory = lambda *a, **k: _directory()
    with routes._directory_lock:
        routes._directory, routes._directory_checked_on = _directory(), date.today()
    routes._check_portfolio_rate = lambda *a, **k: None
    return routes


def _directory():
    from egoquant.agents.market_intel import directory as directory_mod
    return directory_mod.load_directory()


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def process_memory_mb() -> tuple[float, float]:
    """(current working set, peak working set) of this process in MB (Windows; RSS-like)."""
    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p                 # pseudo-handle; 64-bit safe
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return pmc.WorkingSetSize / 2**20, pmc.PeakWorkingSetSize / 2**20
