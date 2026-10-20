"""Fit the market-regime model OFFLINE and save it as a versioned JSON artifact (dev/eval only, never at runtime).

What it does, in plain words
  1. Reads the daily adjusted closes of SPY (a broad US stock market fund) from EgoQuant's price cache
     (EGOQUANT_CACHE_DIR, default ~/.cache/egoquant). Only with --fetch-if-missing, and only when SPY is not cached,
     it asks Tiingo for SPY ONCE (key from TIINGO_API_KEY; the key is never printed or saved).
  2. Turns prices into two daily features: the day's log return and the 21-day realised volatility (standard
     deviation of the last 21 log returns). Both are standardised (minus their mean, divided by their standard
     deviation over the fitting period); the mean and deviation are saved so the runtime can do exactly the same.
  3. Fits a 3-state Gaussian Hidden Markov Model (hmmlearn GaussianHMM, full covariances, fixed random_state).
     An HMM assumes the market is always in one of a few hidden "regimes"; each regime has its own typical daily
     return and volatility, and the regime tomorrow depends only on the regime today (a transition matrix). Nobody
     labels the days: the model finds the regimes itself (unsupervised machine learning).
  4. Sorts the three states by volatility, so state 0 = calm, 1 = stressed, 2 = crisis, and labels every day with
     its most likely regime (Viterbi path).
  5. Writes egoquant/data/regime_model_v1.json: model version, fit date range, the standardisation parameters,
     start probabilities, transition matrix, state means and covariances, the label of every day (run-length
     encoded) and a SHA-256 of the contents.

The backend only LOADS this file and uses numpy (egoquant/regime/). It never fits, never imports hmmlearn and never
calls Tiingo for this feature.

Install (hmmlearn has no wheels for Python 3.14+; use a separate Python 3.9-3.13 environment):
    python -m venv fitenv && fitenv/Scripts/pip install -r eval/regime/requirements-fit.txt

Run:
    python scripts/fit_regime_model.py                                   # -> egoquant/data/regime_model_v1.json
    python scripts/fit_regime_model.py --walk-forward 2012-06-30 2021-06-30 --out-dir eval/results/regime_walkforward
        # one model per quarter-end, each fitted ONLY on data before that date (calibration backtest)

Version 2 (eval/regime/PROTOCOL_v2.md): --variant spy | joint
  * the number of states is chosen by the data: K = 2, 3 and 4 are fitted, each with RESTARTS EM restarts (the best
    log-likelihood kept), and K is chosen by BIC on the fit's own training data (never by a backtest score);
  * `joint` adds AGG (US aggregate bonds) to SPY: 4 features (each fund's log return and 21-day realised vol), so the
    regimes also see the bond market. States are still sorted by SPY volatility (the most volatile = crisis).
  * prices come from the cache only; version 2 never calls Tiingo.
    python scripts/fit_regime_model.py --variant joint                   # -> egoquant/data/regime_model_v2.json
    python scripts/fit_regime_model.py --variant joint --walk-forward 2012-06-30 2021-06-30
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.request
import warnings
from pathlib import Path

# One BLAS thread: multithreaded sums change the last digits between runs, and the artifact must be reproducible.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "egoquant" / "data" / "regime_model_v1.json"
MODEL_VERSION = "regime_hmm_v1"
TICKER = "SPY"
N_STATES = 3
SEED = 20261017                     # the goal simulation's own seed
VOL_WINDOW = 21                     # trading days in the realised-volatility window (one block of the simulation)
STATE_NAMES = ["calm", "stressed", "crisis"]
N_ITER = 1000
TOL = 1e-4
DIGITS = 12                         # significant digits kept in the artifact (far below the fit's own precision)

V2_VERSION = "regime_hmm_v2"
V2_OUT = ROOT / "egoquant" / "data" / "regime_model_v2.json"
V2_VARIANTS = {"spy": ("SPY",), "joint": ("SPY", "AGG")}      # pre-registered in eval/regime/PROTOCOL_v2.md
V2_STATES = (2, 3, 4)
RESTARTS = 10
V2_STATE_NAMES = {2: ["calm", "crisis"], 3: ["calm", "stressed", "crisis"], 4: ["calm", "normal", "stressed", "crisis"]}


# ------------------------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------------------------
def cache_dir() -> Path:
    raw = os.environ.get("EGOQUANT_CACHE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "egoquant"


def load_cached_prices(directory: Path, ticker: str = TICKER) -> tuple[pd.Series, dict] | None:
    """Adjusted closes (SPY by default) from the price cache file written by Agent C's CachedPriceSource."""
    path = directory / "tiingo" / f"{ticker}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    prices = pd.Series(payload["prices"], dtype=float)
    prices.index = pd.to_datetime(prices.index)
    meta = {"source": "EgoQuant price cache (Tiingo adjusted close)", "cache_start": payload.get("start"),
            "cache_end": payload.get("end"), "fetched_on": payload.get("fetched_on")}
    return prices.sort_index(), meta


def fetch_once_from_tiingo() -> tuple[pd.Series, dict]:
    """ONE request for SPY's full daily history. The key is read from the environment and never printed."""
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not key:
        raise SystemExit("SPY is not cached and TIINGO_API_KEY is not set.")
    url = f"https://api.tiingo.com/tiingo/daily/{TICKER}/prices?startDate=1993-01-01&format=json"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:          # noqa: S310 (fixed https URL)
        rows = json.loads(resp.read().decode())
    prices = pd.Series({r["date"][:10]: float(r["adjClose"]) for r in rows}, dtype=float)
    prices.index = pd.to_datetime(prices.index)
    return prices.sort_index(), {"source": "Tiingo adjusted close (one request)", "fetched_on": str(dt.date.today())}


# ------------------------------------------------------------------------------------------------------------
# Features (the runtime, egoquant/regime/model.py, computes exactly the same with numpy)
# ------------------------------------------------------------------------------------------------------------
def raw_features(prices: pd.Series) -> pd.DataFrame:
    """Daily log return and 21-day realised volatility (population std of the last 21 log returns)."""
    r = np.log(prices).diff().dropna()
    vol = r.rolling(VOL_WINDOW).std(ddof=0)
    return pd.DataFrame({"log_return": r, "realised_vol_21d": vol}).dropna()


# ------------------------------------------------------------------------------------------------------------
# Fit
# ------------------------------------------------------------------------------------------------------------
def fit(prices: pd.Series, data_meta: dict, walk_forward_as_of: str | None = None) -> dict:
    from hmmlearn.hmm import GaussianHMM          # dev/eval only

    feats = raw_features(prices)
    mean = feats.mean().to_numpy()
    std = feats.std(ddof=0).to_numpy()
    x = (feats.to_numpy() - mean) / std

    model = GaussianHMM(n_components=N_STATES, covariance_type="full", random_state=SEED, n_iter=N_ITER, tol=TOL)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(x)
    labels = model.predict(x)                                  # Viterbi: the most likely regime of every day
    loglik = float(model.score(x))

    # Sort by volatility: each state's own standard deviation of daily returns, in real units.
    ret_sd = np.sqrt(model.covars_[:, 0, 0]) * std[0]
    order = np.argsort(ret_sd)                                 # order[new] = old
    inverse = np.empty_like(order)
    inverse[order] = np.arange(N_STATES)                       # inverse[old] = new
    startprob = model.startprob_[order]
    trans = model.transmat_[np.ix_(order, order)]
    trans = trans / trans.sum(axis=1, keepdims=True)
    trans = np.array([[_g(v) for v in row] for row in trans])
    trans = trans / trans.sum(axis=1, keepdims=True)                 # rows sum to 1 after rounding
    means = model.means_[order]
    covars = model.covars_[order]
    labels = inverse[labels]

    dates = feats.index
    segments = []
    for i, s in enumerate(labels):
        if i == 0 or s != labels[i - 1]:
            segments.append([str(dates[i].date()), int(s)])
    counts = np.bincount(labels, minlength=N_STATES)
    summary = []
    for k in range(N_STATES):
        r_k = feats["log_return"].to_numpy()[labels == k]
        summary.append({
            "state": k, "name": STATE_NAMES[k], "days": int(counts[k]), "share": round(float(counts[k] / len(labels)), 6),
            "ann_vol": round(float(r_k.std() * np.sqrt(252)), 6) if len(r_k) else 0.0,
            "ann_mean_log_return": round(float(r_k.mean() * 252), 6) if len(r_k) else 0.0,
            "expected_spell_days": round(float(1.0 / max(1e-12, 1.0 - trans[k, k])), 3),
        })

    import hmmlearn
    body = {
        "model_version": MODEL_VERSION,
        "format": 1,
        "kind": "gaussian_hmm",
        "market_proxy": TICKER,
        "data": data_meta,
        "fit_start": str(dates[0].date()),
        "fit_end": str(dates[-1].date()),
        "n_days": int(len(dates)),
        "walk_forward_as_of": walk_forward_as_of,
        "n_components": N_STATES,
        "covariance_type": "full",
        "random_state": SEED,
        "created_with": {"script": "scripts/fit_regime_model.py", "hmmlearn": hmmlearn.__version__,
                         "numpy": np.__version__, "pandas": pd.__version__},
        "features": {"names": ["log_return", "realised_vol_21d"], "vol_window": VOL_WINDOW, "vol_ddof": 0,
                     "mean": [_g(v) for v in mean], "std": [_g(v) for v in std]},
        "state_names": STATE_NAMES,
        "start_probs": [_g(v) for v in startprob],
        "transition_matrix": [[float(v) for v in row] for row in trans],
        "means": [[_g(v) for v in row] for row in means],
        "covariances": [[[_g(v) for v in row] for row in c] for c in covars],
        "state_summary": summary,
        "fit": {"log_likelihood": round(loglik, 4), "n_iter": int(model.monitor_.iter), "converged": bool(model.monitor_.converged),
                "labelling": "Viterbi path (most likely regime sequence)"},
        "labels": {"encoding": "run_length", "first_date": str(dates[0].date()), "last_date": str(dates[-1].date()),
                   "n_days": int(len(dates)), "counts": [int(c) for c in counts], "segments": segments,
                   "note": "segments: [first day, state]; a day's state is that of the last segment starting on or "
                           "before it"},
    }
    body["sha256"] = content_hash(body)
    return body


def _g(v) -> float:
    """A float rounded to DIGITS significant digits, so tiny floating-point noise never changes the artifact."""
    return float(f"{float(v):.{DIGITS}g}")


def content_hash(body: dict) -> str:
    """SHA-256 of the canonical JSON of every field except `sha256` (egoquant/regime/model.py checks the same)."""
    clean = {k: v for k, v in body.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def write(body: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


def describe(body: dict) -> str:
    lines = [f"{body['model_version']}{' ' + body['variant'] if body.get('variant') else ''}  "
             f"K={body['n_components']}  fit {body['fit_start']} .. {body['fit_end']} ({body['n_days']} days)  "
             f"converged={body['fit']['converged']} iter={body['fit']['n_iter']}  sha256={body['sha256'][:16]}"]
    for s in body["state_summary"]:
        lines.append(f"  {s['state']} {s['name']:<8} {s['days']:>5} days ({s['share']:.1%})  vol {s['ann_vol']:.1%}/yr  "
                     f"mean {s['ann_mean_log_return']:+.1%}/yr  expected spell {s['expected_spell_days']:.0f} days")
    lines.append("  P = " + "; ".join(" ".join(f"{v:.4f}" for v in row) for row in body["transition_matrix"]))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------
# Version 2: K chosen by BIC, EM restarts, optional bond features
# ------------------------------------------------------------------------------------------------------------
def joint_features(prices: dict, tickers) -> pd.DataFrame:
    """[t1 log return, t1 vol, t2 log return, t2 vol, ...] on the days every fund has (inner join)."""
    parts = [raw_features(prices[t]).add_prefix(f"{t}:") for t in tickers]
    return pd.concat(parts, axis=1, join="inner").dropna()


def n_parameters(k: int, d: int) -> int:
    """Free parameters of a K-state full-covariance Gaussian HMM on d features (for BIC)."""
    return (k - 1) + k * (k - 1) + k * d + k * d * (d + 1) // 2


def best_of_restarts(x: np.ndarray, k: int):
    """The restart with the highest log-likelihood (ties: the earliest restart)."""
    from hmmlearn.hmm import GaussianHMM          # dev/eval only
    best = None
    for i in range(RESTARTS):
        m = GaussianHMM(n_components=k, covariance_type="full", random_state=SEED + i, n_iter=N_ITER, tol=TOL)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                m.fit(x)
                ll = float(m.score(x))
            except (ValueError, np.linalg.LinAlgError):
                continue
        if not np.isfinite(ll):
            continue
        if best is None or ll > best[1] + 1e-9:
            best = (m, ll, i)
    if best is None:
        raise RuntimeError(f"no restart converged for K={k}")
    return best


def fit_v2(prices: dict, variant: str, data_meta: dict, walk_forward_as_of: str | None = None) -> dict:
    tickers = V2_VARIANTS[variant]
    feats = joint_features(prices, tickers)
    mean = feats.mean().to_numpy()
    std = feats.std(ddof=0).to_numpy()
    x = (feats.to_numpy() - mean) / std
    d = x.shape[1]
    candidates, fitted = [], {}
    for k in V2_STATES:
        m, ll, restart = best_of_restarts(x, k)
        bic = -2.0 * ll + n_parameters(k, d) * np.log(len(x))
        fitted[k] = m
        candidates.append({"n_components": k, "log_likelihood": round(ll, 4), "bic": round(float(bic), 4),
                           "parameters": n_parameters(k, d), "best_restart": restart,
                           "converged": bool(m.monitor_.converged), "n_iter": int(m.monitor_.iter)})
    chosen = min(candidates, key=lambda c: (c["bic"], c["n_components"]))
    k = chosen["n_components"]
    model = fitted[k]
    labels = model.predict(x)

    ret_sd = np.sqrt(model.covars_[:, 0, 0]) * std[0]          # SPY's daily return volatility in each state
    order = np.argsort(ret_sd, kind="stable")
    inverse = np.empty_like(order)
    inverse[order] = np.arange(k)
    trans = model.transmat_[np.ix_(order, order)]
    trans = trans / trans.sum(axis=1, keepdims=True)
    trans = np.array([[_g(v) for v in row] for row in trans])
    trans = trans / trans.sum(axis=1, keepdims=True)
    startprob = np.array([_g(v) for v in model.startprob_[order]])
    startprob = startprob / startprob.sum()
    means = model.means_[order]
    covars = model.covars_[order]
    labels = inverse[labels]
    names = V2_STATE_NAMES[k]

    dates = feats.index
    segments = []
    for i, s in enumerate(labels):
        if i == 0 or s != labels[i - 1]:
            segments.append([str(dates[i].date()), int(s)])
    counts = np.bincount(labels, minlength=k)
    spy_r = feats[f"{tickers[0]}:log_return"].to_numpy()
    summary = []
    for j in range(k):
        r_j = spy_r[labels == j]
        summary.append({
            "state": j, "name": names[j], "days": int(counts[j]), "share": round(float(counts[j] / len(labels)), 6),
            "ann_vol": round(float(r_j.std() * np.sqrt(252)), 6) if len(r_j) else 0.0,
            "ann_mean_log_return": round(float(r_j.mean() * 252), 6) if len(r_j) else 0.0,
            "expected_spell_days": round(float(1.0 / max(1e-12, 1.0 - trans[j, j])), 3),
        })

    import hmmlearn
    body = {
        "model_version": V2_VERSION,
        "format": 2,
        "kind": "gaussian_hmm",
        "variant": variant,
        "market_proxy": tickers[0],
        "data": data_meta,
        "fit_start": str(dates[0].date()),
        "fit_end": str(dates[-1].date()),
        "n_days": int(len(dates)),
        "walk_forward_as_of": walk_forward_as_of,
        "n_components": k,
        "covariance_type": "full",
        "random_state": SEED,
        "created_with": {"script": "scripts/fit_regime_model.py", "hmmlearn": hmmlearn.__version__,
                         "numpy": np.__version__, "pandas": pd.__version__},
        "features": {"tickers": list(tickers), "per_ticker": ["log_return", "realised_vol_21d"],
                     "names": [f"{t}:{n}" for t in tickers for n in ("log_return", "realised_vol_21d")],
                     "vol_window": VOL_WINDOW, "vol_ddof": 0,
                     "mean": [_g(v) for v in mean], "std": [_g(v) for v in std]},
        "state_names": names,
        "start_probs": [float(v) for v in startprob],
        "transition_matrix": [[float(v) for v in row] for row in trans],
        "means": [[_g(v) for v in row] for row in means],
        "covariances": [[[_g(v) for v in row] for row in c] for c in covars],
        "state_summary": summary,
        "selection": {"criterion": "BIC on the training data (lower is better)", "states_tried": list(V2_STATES),
                      "restarts_per_k": RESTARTS, "restart_seeds": f"{SEED} + 0..{RESTARTS - 1}",
                      "candidates": candidates, "chosen_n_components": k,
                      "protocol": "eval/regime/PROTOCOL_v2.md"},
        "fit": {"log_likelihood": chosen["log_likelihood"], "n_iter": chosen["n_iter"],
                "converged": chosen["converged"], "labelling": "Viterbi path (most likely regime sequence)"},
        "labels": {"encoding": "run_length", "first_date": str(dates[0].date()), "last_date": str(dates[-1].date()),
                   "n_days": int(len(dates)), "counts": [int(c) for c in counts], "segments": segments,
                   "note": "segments: [first day, state]; a day's state is that of the last segment starting on or "
                           "before it"},
    }
    body["sha256"] = content_hash(body)
    return body


def quarter_ends(start: str, end: str) -> list[pd.Timestamp]:
    return list(pd.date_range(start, end, freq="QE"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache-dir", type=Path, default=None, help="default: EGOQUANT_CACHE_DIR or ~/.cache/egoquant")
    ap.add_argument("--fetch-if-missing", action="store_true",
                    help="only if SPY is not cached: ONE Tiingo request for SPY (key from TIINGO_API_KEY)")
    ap.add_argument("--walk-forward", nargs=2, metavar=("START", "END"),
                    help="fit one model per quarter-end in [START, END], each on data BEFORE that date")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "eval" / "results" / "regime_walkforward")
    ap.add_argument("--variant", choices=["v1", *V2_VARIANTS], default="v1",
                    help="v1 (3 states, SPY, one run) or a version-2 variant (eval/regime/PROTOCOL_v2.md)")
    args = ap.parse_args(argv)

    if args.variant != "v1":
        return main_v2(args)

    loaded = load_cached_prices(args.cache_dir or cache_dir())
    if loaded is None:
        if not args.fetch_if_missing:
            raise SystemExit(f"{TICKER} is not in the price cache; rerun with --fetch-if-missing (one Tiingo request).")
        loaded = fetch_once_from_tiingo()
    prices, meta = loaded

    if args.walk_forward:
        for t in quarter_ends(*args.walk_forward):
            body = fit(prices[prices.index < t], meta, walk_forward_as_of=str(t.date()))
            write(body, args.out_dir / f"{MODEL_VERSION}_asof_{t.date()}.json")
            print(describe(body))
        return 0

    body = fit(prices, meta)
    write(body, args.out)
    print(describe(body))
    print(f"written: {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 0


def main_v2(args) -> int:
    """Version 2: cached prices only (every ticker of the variant must be cached; no Tiingo request)."""
    directory = args.cache_dir or cache_dir()
    prices, meta = {}, {}
    for t in V2_VARIANTS[args.variant]:
        loaded = load_cached_prices(directory, t)
        if loaded is None:
            raise SystemExit(f"{t} is not in the price cache (version 2 never fetches).")
        prices[t], meta[t] = loaded
    if args.walk_forward:
        for t in quarter_ends(*args.walk_forward):
            body = fit_v2({k: v[v.index < t] for k, v in prices.items()}, args.variant, meta,
                          walk_forward_as_of=str(t.date()))
            write(body, args.out_dir / f"{V2_VERSION}_{args.variant}_asof_{t.date()}.json")
            print(describe(body))
        return 0
    out = args.out if args.out != DEFAULT_OUT else V2_OUT
    body = fit_v2(prices, args.variant, meta)
    write(body, out)
    print(describe(body))
    print("  BIC: " + "; ".join(f"K={c['n_components']} {c['bic']:.1f}" for c in body["selection"]["candidates"]))
    print(f"written: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
