"""Runtime settings: data-vendor credentials and the local price cache.

Credentials come ONLY from the environment (never from the code):
    TIINGO_API_KEY       Tiingo key for real end-of-day ETF prices (Agent C). Required for real ETF prices;
                         offline runs use synthetic or CSV prices and need no key.
    EGOQUANT_CACHE_DIR   where fetched prices are cached (default: ~/.cache/egoquant)

A local `.env` file in the repository root is read once at import (it is gitignored; see .env.example).
Variables already set in the environment win over `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _load_env_file() -> None:
    try:
        from dotenv import load_dotenv      # optional (python-dotenv); without it, export the variables
    except ImportError:
        return
    if _ENV_FILE.is_file():
        load_dotenv(_ENV_FILE, override=False)


_load_env_file()

TIINGO_KEY_HELP = ("TIINGO_API_KEY is not set. Real ETF prices come from Tiingo: put your key in the .env file "
                   "(see .env.example) or export TIINGO_API_KEY. Offline runs can use synthetic prices "
                   "(--synthetic / SyntheticPriceSource) or a CSV file and need no key")   # no final period: Agent C appends


def tiingo_api_key() -> str:
    """The traders' Tiingo key: Agent C's live fetches for each portfolio build. "" if not set."""
    return os.environ.get("TIINGO_API_KEY", "").strip()


def tiingo_warm_api_key() -> str:
    """The background builder's Tiingo key (TIINGO_WARM_API_KEY). Without one, the builder shares the
    traders' key (and then leaves them part of its hourly allowance, see warm_per_hour)."""
    return os.environ.get("TIINGO_WARM_API_KEY", "").strip() or tiingo_api_key()


def warm_has_own_key() -> bool:
    return bool(os.environ.get("TIINGO_WARM_API_KEY", "").strip()) and tiingo_warm_api_key() != tiingo_api_key()


def data_fallback() -> str:
    """EGOQUANT_DATA_FALLBACK=synthetic: if a live price feed fails (no key, key refused, rate limit,
    blocked region), Agent C switches the whole run to SYNTHETIC prices, labeled as such. Off by default:
    real data is never silently replaced."""
    return os.environ.get("EGOQUANT_DATA_FALLBACK", "").strip().lower()


def price_mode() -> Literal["live", "snapshot"]:
    """EGOQUANT_PRICE_MODE: "snapshot" = every build reads the committed snapshot of REAL prices directly and makes no live
    request at all (no Tiingo, no Binance, no directory download, no background price builder); anything else, including
    unset, is "live" (today's behaviour: live feeds first, the snapshot only when they fail). Needs the snapshot to be
    on (EGOQUANT_PRICE_SNAPSHOT) and recent (30 days); otherwise Agent C quietly uses the live feeds."""
    return "snapshot" if os.environ.get("EGOQUANT_PRICE_MODE", "").strip().lower() == "snapshot" else "live"


def live_budget_seconds() -> float:
    """EGOQUANT_LIVE_BUDGET_SECONDS (default 15, PROVISIONAL): the most time one build may spend waiting for LIVE price
    requests. Past it the WHOLE build switches to the committed snapshot (one source per run)."""
    try:
        return max(1.0, float(os.environ.get("EGOQUANT_LIVE_BUDGET_SECONDS", "15")))
    except ValueError:
        return 15.0


def slow_request_seconds() -> float:
    """EGOQUANT_SLOW_REQUEST_SECONDS (default 8, PROVISIONAL): a single live request slower than this counts as slow;
    two slow requests in one build switch the whole build to the snapshot."""
    try:
        return max(0.5, float(os.environ.get("EGOQUANT_SLOW_REQUEST_SECONDS", "8")))
    except ValueError:
        return 8.0


def max_concurrent_builds() -> int:
    """EGOQUANT_MAX_CONCURRENT_BUILDS (default 1, PROVISIONAL): portfolio builds that run at the same time in this process.
    Measured 20 Oct (damla_hostile/conc.py flood, 20 heavy goal builds at once): builds share one CPU (the GIL), so more at once
    adds memory (about 150 MB each; 3.2 GB for 20) and waiting but no speed: 1 slot served all 20 in 89 s, 3 slots took 130 s and
    turned 6 away. Concurrent pandas work also crashed or hung the process on the development PC (pandas 3.0.6, numpy 2.5.4,
    Python 3.13.5): without the gate 3 of 3 floods killed it, with 3 slots 2 of 4 hung, with 1 or 2 slots and one BLAS thread
    all runs finished. The others queue (build_wait_seconds)."""
    try:
        return max(1, int(os.environ.get("EGOQUANT_MAX_CONCURRENT_BUILDS", "1")))
    except ValueError:
        return 1


def build_wait_seconds() -> float:
    """EGOQUANT_BUILD_WAIT_SECONDS (default 120, PROVISIONAL): how long a build waits for a free slot before the
    answer is a 503 "busy, retry" (the host's proxy gives up near 250 s)."""
    try:
        return max(0.0, float(os.environ.get("EGOQUANT_BUILD_WAIT_SECONDS", "120")))
    except ValueError:
        return 120.0


def warm_enabled() -> bool:
    """EGOQUANT_WARM=off stops the background cache builder (market_intel/warm.py). On by default when the
    service has a Tiingo key and real prices (with only the traders' key, see warm_may_share_key). Always off in
    snapshot price mode: nothing is fetched live then."""
    if price_mode() == "snapshot":
        return False
    return os.environ.get("EGOQUANT_WARM", "").strip().lower() not in {"off", "0", "false", "no"}


def warm_may_share_key() -> bool:
    """EGOQUANT_WARM=shared lets the builder run on the traders' key when it has none of its own. Off by default:
    its per-process request count restarts at 0 on every redeploy, so on one shared key it used up Tiingo's 50
    requests an hour together with the first portfolio builds (Oct 19), and traders then got the snapshot only."""
    return os.environ.get("EGOQUANT_WARM", "").strip().lower() == "shared"


def warm_per_hour() -> int:
    """EGOQUANT_WARM_PER_HOUR: the builder works only while fewer requests than this were made on ITS key in
    the last hour. Default 45 of the free tier's 50 with its own key; 30 when it shares the traders' key
    (their requests count too, so traders keep the rest)."""
    default = 45 if warm_has_own_key() else 30
    try:
        return max(0, int(os.environ.get("EGOQUANT_WARM_PER_HOUR", str(default))))
    except ValueError:
        return default


def warm_per_cluster() -> int:
    """EGOQUANT_WARM_PER_CLUSTER: most traded funds per cluster the builder keeps warm. Default 10."""
    try:
        return max(1, int(os.environ.get("EGOQUANT_WARM_PER_CLUSTER", "10")))
    except ValueError:
        return 10


def price_memory_cache_files() -> int:
    """EGOQUANT_PRICE_MEMORY_FILES: price-cache files Agent C keeps in memory (least recently used dropped first;
    the disk cache keeps everything). PROVISIONAL default 250, ~0.23 MB each (~58 MB): more than one build's
    universe (up to ~130 funds), well inside the 512 MB server. It used to grow without limit (Z7 soak: 593 files).
    Portfolio method v2 prices every eligible product at once (the catalog has 292), so its default is 400
    (MAX_UNIVERSE): the cache is kept as compact arrays, about 60 KB a product for 20 years, so 400 files are ~25 MB."""
    default = 400 if portfolio_method() == "v2" else 250
    try:
        return max(1, int(os.environ.get("EGOQUANT_PRICE_MEMORY_FILES", str(default))))
    except ValueError:
        return default


def price_snapshot_enabled() -> bool:
    """EGOQUANT_PRICE_SNAPSHOT=off disables the committed snapshot of REAL prices that Agent C uses when
    a live feed fails (see egoquant/agents/market_intel/snapshot.py). On by default: it is real data,
    labeled with the date it runs to."""
    return os.environ.get("EGOQUANT_PRICE_SNAPSHOT", "").strip().lower() not in {"off", "0", "false", "no"}


def portfolio_method() -> Literal["v1", "v2"]:
    """EGOQUANT_PORTFOLIO_METHOD: which portfolio engine builds the candidates (docs/PORTFOLIO_V2.md). "v2" only when
    the variable, stripped and lower-cased, is exactly "v2"; anything else, including unset, empty or "V2 " typos like
    "v2.0", is "v1" (today's engine, untouched). Read it here and nowhere else; the Agent A and Agent C code call this."""
    return "v2" if os.environ.get("EGOQUANT_PORTFOLIO_METHOD", "").strip().lower() == "v2" else "v1"


def risk_game_weight() -> float | None:
    """EGOQUANT_RISK_GAME_WEIGHT: the share of the Risk Game in the TOLERANCE part of the risk category (docs/RISK_GAME_BLEND.md).
    Unset, empty or not a number = None (the risk profile's own default, one third). A number is clamped to 0..0.5; 0 means the
    game is ignored everywhere (the kill switch)."""
    raw = os.environ.get("EGOQUANT_RISK_GAME_WEIGHT", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return min(0.5, max(0.0, value))


def risk_game_may_raise() -> bool | None:
    """EGOQUANT_RISK_GAME_MAY_RAISE: `off` (also 0, false, no) lets the game only LOWER the tolerance part of the category; any
    other value or unset = None (the risk profile's default: the game may also raise it, never above the capacity cap)."""
    off = os.environ.get("EGOQUANT_RISK_GAME_MAY_RAISE", "").strip().lower() in {"off", "0", "false", "no"}
    return False if off else None


def risk_game_options() -> dict:
    """The keyword arguments of build_risk_profile that come from the environment (empty = the profile's defaults)."""
    options: dict = {}
    weight, may_raise = risk_game_weight(), risk_game_may_raise()
    if weight is not None:
        options["game_weight"] = weight
    if may_raise is not None:
        options["may_raise"] = may_raise
    return options


def cache_dir() -> Path:
    return Path(os.environ.get("EGOQUANT_CACHE_DIR") or Path.home() / ".cache" / "egoquant")
