"""The fitted market-regime model at runtime: load, validate, filter. numpy only; never fits, never calls a vendor.

The model is a 3-state Gaussian Hidden Markov Model (HMM) fitted OFFLINE by scripts/fit_regime_model.py on SPY's
daily log return and 21-day realised volatility (both standardised). Its states are sorted by volatility:
0 = calm, 1 = stressed, 2 = crisis. The artifact (egoquant/data/regime_model_v1.json) holds the transition matrix
P (P[i, j] = chance that tomorrow is regime j when today is regime i), each regime's feature mean and covariance,
and the regime of every trading day it was fitted on.

Version 2 (regime_model_v2.json, eval/regime/PROTOCOL_v2.md) has the same layout with 2 to 4 states (chosen by BIC
when it was fitted) and may use several funds' features (variant "joint": SPY and AGG, each fund's log return and
21-day realised vol). The most volatile state is always "crisis". Both versions load through the same code; the
model a report used is found again by its version (model_path_for), so a report replays with its own model.

    load_regime_model()                 the validated artifact (cached in memory); RegimeModelError if unusable
    RegimeModel.labels_on(dates)        regime of each date (-1 outside the labelled range)
    current_regime_probs(returns, m)    today's regime probabilities: the HMM forward filter over recent SPY returns
    stationary_distribution(P)          the long-run share of days in each regime (used when no recent data exists)
    block_transition_matrix(P, 21)      P^21: regime-to-regime chances one simulated month (21 trading days) later
    recent_market_returns(as_of)        SPY daily log returns up to as_of from local files only (price cache, then the
                                        committed real-price snapshot); None if neither has them
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODEL_VERSION = "regime_hmm_v1"
MODEL_PATH = Path(__file__).resolve().parents[1] / "data" / "regime_model_v1.json"
MODEL_PATHS = {MODEL_VERSION: MODEL_PATH,
               "regime_hmm_v2": Path(__file__).resolve().parents[1] / "data" / "regime_model_v2.json"}
STATE_NAMES = ("calm", "stressed", "crisis")
N_STATES = 3                # version 1
N_FEATURES = 2              # version 1 (and per fund in version 2)
V2_STATES = (2, 3, 4)
MARKET_PROXY = "SPY"
RECENT_DAYS = 252          # PROVISIONAL: trading days of recent SPY returns the forward filter runs over (~1 year)
ROW_TOLERANCE = 1e-9       # rows of the transition matrix must sum to 1 within this


class RegimeModelError(ValueError):
    """The artifact cannot be used. `reason` is a short safe label (artifact_missing | artifact_invalid |
    artifact_hash_mismatch); the regime sampler then falls back to the block bootstrap."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class RegimeModel:
    version: str
    sha256: str
    fit_start: date
    fit_end: date
    feature_mean: np.ndarray            # (d,)  standardisation of [log return, 21-day realised vol] per fund
    feature_std: np.ndarray             # (d,)
    vol_window: int
    start_probs: np.ndarray             # (K,)  the fit's own start distribution (2007; not used for "today")
    transition: np.ndarray              # (K, K) daily transition matrix P
    means: np.ndarray                   # (K, d) regime means of the standardised features
    covariances: np.ndarray             # (K, d, d)
    segment_starts: np.ndarray          # datetime64[D], first day of each run of equal labels
    segment_states: np.ndarray          # int8, the regime of each run
    state_names: tuple[str, ...] = STATE_NAMES
    summary: tuple = field(default_factory=tuple)
    tickers: tuple[str, ...] = (MARKET_PROXY,)      # the funds whose features the model reads, in feature order

    @property
    def n_states(self) -> int:
        return len(self.transition)

    def labels_on(self, dates) -> np.ndarray:
        """Regime of each date (int8; -1 before the first or after the last labelled day). A date that is not a US
        trading day (a crypto weekend) gets the regime of the last trading day before it."""
        d = np.asarray(dates, dtype="datetime64[D]")
        pos = np.searchsorted(self.segment_starts, d, side="right") - 1
        out = self.segment_states[np.clip(pos, 0, None)].astype(np.int8)
        outside = (d < np.datetime64(self.fit_start)) | (d > np.datetime64(self.fit_end))
        out[outside] = -1
        return out


# --------------------------------------------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int], RegimeModel]] = {}


def content_hash(body: dict) -> str:
    """SHA-256 of the canonical JSON of every field except `sha256` (the same rule the fitting script uses)."""
    clean = {k: v for k, v in body.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def model_path_for(version: str | None) -> Path | None:
    """The committed artifact of a model version (None if the version is unknown)."""
    return MODEL_PATHS.get(version) if version else None


def load_regime_model(path: str | Path | None = None) -> RegimeModel:
    """The validated artifact, read once and kept in memory (re-read only if the file changes)."""
    p = Path(path) if path is not None else MODEL_PATH
    try:
        st = p.stat()
    except OSError:
        raise RegimeModelError("artifact_missing", p.name) from None
    sig = (st.st_mtime_ns, st.st_size)
    key = str(p)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegimeModelError("artifact_invalid", type(exc).__name__) from None
    model = parse_regime_model(body)
    with _lock:
        _cache[key] = (sig, model)
    return model


def parse_regime_model(body) -> RegimeModel:
    """Check the artifact (hash, version, shapes, probabilities, covariances, labels) and build the model."""
    if not isinstance(body, dict):
        raise RegimeModelError("artifact_invalid", "not a JSON object")
    if body.get("sha256") != content_hash(body):
        raise RegimeModelError("artifact_hash_mismatch")
    try:
        version = body["model_version"]
        k = int(body["n_components"])
        feats = body["features"]
        if version == MODEL_VERSION:
            if k != N_STATES:
                raise ValueError("unexpected number of states")
            tickers = (MARKET_PROXY,)
            names_default = STATE_NAMES
        elif version in MODEL_PATHS:
            if k not in V2_STATES:
                raise ValueError("unexpected number of states")
            tickers = tuple(str(t) for t in feats["tickers"])
            if not tickers or tickers[0] != MARKET_PROXY or len(set(tickers)) != len(tickers):
                raise ValueError("the first fund must be the market proxy, and funds must be distinct")
            names_default = None
        else:
            raise ValueError("unexpected model version")
        d = N_FEATURES * len(tickers)
        mean = _array(feats["mean"], (d,))
        std = _array(feats["std"], (d,))
        if np.any(std <= 0):
            raise ValueError("feature std must be positive")
        vol_window = int(feats["vol_window"])
        if vol_window < 2:
            raise ValueError("vol_window too small")
        start = _array(body["start_probs"], (k,))
        trans = _array(body["transition_matrix"], (k, k))
        means = _array(body["means"], (k, d))
        covs = _array(body["covariances"], (k, d, d))
        if np.any(start < 0) or abs(start.sum() - 1.0) > 1e-6:
            raise ValueError("start probabilities must be >= 0 and sum to 1")
        if np.any(trans < 0) or np.any(np.abs(trans.sum(axis=1) - 1.0) > ROW_TOLERANCE):
            raise ValueError("transition matrix rows must be >= 0 and sum to 1")
        for c in covs:
            if not np.allclose(c, c.T, atol=1e-12):
                raise ValueError("covariance not symmetric")
            np.linalg.cholesky(c)                       # raises LinAlgError unless positive definite
        labels = body["labels"]
        segs = labels["segments"]
        if not segs:
            raise ValueError("no labels")
        starts = np.array([s[0] for s in segs], dtype="datetime64[D]")
        states = np.array([int(s[1]) for s in segs], dtype=np.int64)
        if np.any(np.diff(starts) <= np.timedelta64(0, "D")) or np.any((states < 0) | (states >= k)):
            raise ValueError("label segments must be increasing dates with states 0..K-1")
        fit_start, fit_end = date.fromisoformat(body["fit_start"]), date.fromisoformat(body["fit_end"])
        if np.datetime64(fit_start) != starts[0] or np.datetime64(fit_end) < starts[-1]:
            raise ValueError("label range does not match the fit range")
        names = tuple(body.get("state_names") or names_default or ())
        if len(names) != k or names[-1] != "crisis":
            raise ValueError("state names")
    except RegimeModelError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, np.linalg.LinAlgError) as exc:
        raise RegimeModelError("artifact_invalid", str(exc)[:120]) from None
    return RegimeModel(version=body["model_version"], sha256=body["sha256"], fit_start=fit_start, fit_end=fit_end,
                       feature_mean=mean, feature_std=std, vol_window=vol_window, start_probs=start, transition=trans,
                       means=means, covariances=covs, segment_starts=starts, segment_states=states.astype(np.int8),
                       state_names=names, summary=tuple(body.get("state_summary") or ()), tickers=tickers)


def _array(value, shape: tuple[int, ...]) -> np.ndarray:
    arr = np.asarray(value, dtype=float)
    if arr.shape != shape:
        raise ValueError(f"expected shape {shape}, got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("non-finite value")
    return arr


# --------------------------------------------------------------------------------------------------------------
# HMM arithmetic (numpy only)
# --------------------------------------------------------------------------------------------------------------
def stationary_distribution(P: np.ndarray) -> np.ndarray:
    """The long-run share of days in each regime: the row vector pi with pi P = pi and sum(pi) = 1."""
    n = P.shape[0]
    a = np.vstack([P.T - np.eye(n), np.ones((1, n))])
    b = np.zeros(n + 1)
    b[-1] = 1.0
    pi = np.linalg.lstsq(a, b, rcond=None)[0]
    pi = np.clip(pi, 0.0, None)
    return pi / pi.sum()


def block_transition_matrix(P: np.ndarray, block_len: int = 21) -> np.ndarray:
    """P^block_len (matrix power): the regime chances one simulated block later. The block bootstrap moves in whole
    blocks of `block_len` trading days, so the simulated regime can change once per block."""
    if block_len < 1:
        raise ValueError("block_len must be >= 1")
    out = np.linalg.matrix_power(P, int(block_len))
    return out / out.sum(axis=1, keepdims=True)


def features(returns: np.ndarray, model: RegimeModel) -> np.ndarray:
    """Standardised [log return, realised vol] rows (per fund, in the model's fund order), exactly as the fitting
    script builds them. returns: (T,) for one fund or (T, n_funds). The first vol_window - 1 returns only warm up the
    volatility window."""
    r = np.asarray(returns, dtype=float)
    if r.ndim == 1:
        r = r[:, None]
    w = model.vol_window
    if len(r) < w:
        return np.empty((0, N_FEATURES * r.shape[1]))
    cols = []
    for j in range(r.shape[1]):
        windows = np.lib.stride_tricks.sliding_window_view(r[:, j], w)
        cols += [r[w - 1:, j], windows.std(axis=1)]             # population std (ddof 0), as in the fit
    raw = np.column_stack(cols)
    return (raw - model.feature_mean) / model.feature_std


def _log_densities(x: np.ndarray, model: RegimeModel) -> np.ndarray:
    """log N(x_t | mean_k, cov_k) for every day t and regime k: shape (T, K)."""
    out = np.empty((len(x), model.n_states))
    d = x.shape[1]
    for k in range(model.n_states):
        chol = np.linalg.cholesky(model.covariances[k])
        z = np.linalg.solve(chol, (x - model.means[k]).T)
        out[:, k] = -0.5 * (np.sum(z * z, axis=0) + d * np.log(2 * np.pi)) - np.sum(np.log(np.diag(chol)))
    return out


def forward_filter(x: np.ndarray, model: RegimeModel, prior: np.ndarray) -> np.ndarray:
    """Filtered regime probabilities P(regime_t | data up to t) for every row of x (the HMM forward recursion)."""
    logb = _log_densities(x, model)
    probs = np.empty_like(logb)
    alpha = prior
    for t in range(len(x)):
        pred = alpha if t == 0 else alpha @ model.transition
        lik = np.exp(logb[t] - logb[t].max())
        alpha = pred * lik
        total = alpha.sum()
        alpha = alpha / total if total > 0 else pred
        probs[t] = alpha
    return probs


def current_regime_probs(recent_returns, model: RegimeModel, steps_ahead: int = 0) -> tuple[np.ndarray, str]:
    """Today's regime probabilities and how they were found ("filtered" or "stationary").

    recent_returns: daily log returns of the model's funds ((T,) or (T, n_funds)), oldest first, ending on the last
    day with data (None if there are none).
    The forward filter starts from the stationary distribution, runs over the last RECENT_DAYS returns and gives the
    regime probabilities on the last day. steps_ahead: trading days between that day and the decision date; the
    probabilities are moved forward by P^steps_ahead (the model's own forecast of the regime for days it has not seen).
    Without enough data: the stationary distribution (the long-run share of days in each regime). Deterministic.
    """
    pi = stationary_distribution(model.transition)
    if recent_returns is None:
        return pi, "stationary"
    r = np.asarray(recent_returns, dtype=float)[-(RECENT_DAYS + model.vol_window - 1):]
    r = r[np.isfinite(r)] if r.ndim == 1 else r[np.isfinite(r).all(axis=1)]
    x = features(r, model)
    if len(x) == 0:
        return pi, "stationary"
    probs = forward_filter(x, model, pi)[-1]
    if steps_ahead > 0:
        probs = probs @ np.linalg.matrix_power(model.transition, int(steps_ahead))
    probs = np.clip(probs, 0.0, None)
    return probs / probs.sum(), "filtered"


# --------------------------------------------------------------------------------------------------------------
# Recent market data, from local files only
# --------------------------------------------------------------------------------------------------------------
def _latest_prices(ticker: str, as_of: date):
    """(days, closes) up to as_of from the price cache, else the committed snapshot (read only when the cache does not
    reach as_of). None if neither has two days before as_of."""
    best = None
    for load in (_cached_prices, _snapshot_prices):
        if best is not None and _weekdays_after(best[0][-1], as_of) <= 1:
            break                                       # the cache reaches as_of: no need to read the snapshot
        prices = load(ticker)
        if prices is None:
            continue
        days, values = prices
        keep = days <= np.datetime64(as_of)
        if keep.sum() < 2:
            continue
        if best is None or days[keep][-1] > best[0][-1]:
            best = (days[keep], values[keep])
    return best


def recent_market_returns(as_of: date, ticker: str = MARKET_PROXY) -> tuple[np.ndarray, date, int] | None:
    """SPY daily log returns up to `as_of` (inclusive), the last day with data, and the trading days (weekdays)
    from that day to `as_of`. Read from the price cache (EGOQUANT_CACHE_DIR/tiingo/SPY.json), else from the
    committed real-price snapshot. No network, ever. None if neither file has SPY before as_of."""
    best = _latest_prices(ticker, as_of)
    if best is None:
        return None
    days, values = best
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(np.log(values))
    return returns, days[-1].astype(object), _weekdays_after(days[-1], as_of)


def recent_model_returns(as_of: date, model: RegimeModel) -> tuple[np.ndarray, date, int] | None:
    """Like recent_market_returns, for every fund the model reads: (T,) for one fund (exactly
    recent_market_returns), else (T, n_funds) log returns on the days all funds have, from local files only."""
    if len(model.tickers) == 1:
        return recent_market_returns(as_of, model.tickers[0])
    series = []
    for t in model.tickers:
        best = _latest_prices(t, as_of)
        if best is None:
            return None
        series.append(best)
    common = series[0][0]
    for days, _ in series[1:]:
        common = np.intersect1d(common, days)
    if len(common) < 2:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        cols = [np.diff(np.log(values[np.searchsorted(days, common)])) for days, values in series]
    return np.column_stack(cols), common[-1].astype(object), _weekdays_after(common[-1], as_of)


def _weekdays_after(last_day, as_of: date) -> int:
    """Weekdays after last_day up to and including as_of (0 when the data reaches as_of)."""
    gap = np.busday_count(np.datetime64(last_day, "D") + np.timedelta64(1, "D"),
                          np.datetime64(as_of) + np.timedelta64(1, "D"))
    return max(int(gap), 0)


def _cached_prices(ticker: str):
    from egoquant import settings
    path = settings.cache_dir() / "tiingo" / f"{ticker.upper()}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _series(payload["prices"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _snapshot_prices(ticker: str):
    from egoquant import settings
    if not settings.price_snapshot_enabled():
        return None
    try:
        from egoquant.agents.market_intel.snapshot import load_snapshot
        frame = load_snapshot().prices
    except Exception:                                   # missing or unreadable snapshot: no data, never an error
        return None
    if ticker not in frame.columns:
        return None
    s = frame[ticker].dropna()
    return s.index.to_numpy(dtype="datetime64[D]"), s.to_numpy(dtype=float)


def _series(prices: dict):
    """(days, closes) sorted by day; built with numpy directly (no per-day Python tuples kept alive afterwards)."""
    if not prices:
        return None
    days = np.array(list(prices.keys()), dtype="datetime64[D]")
    values = np.array([np.nan if v is None else v for v in prices.values()], dtype=float)
    order = np.argsort(days, kind="stable")
    days, values = days[order], values[order]
    ok = np.isfinite(values) & (values > 0)
    return days[ok], values[ok]
