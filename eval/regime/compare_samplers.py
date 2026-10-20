"""Validation of the regime-aware sampler against the existing block bootstrap (required before turning the flag on).

    python eval/regime/compare_samplers.py            # everything -> eval/results/regime_compare_<ts>.json + .md
    python eval/regime/compare_samplers.py --quick    # fewer paths and dates (a smoke run; not for the decision)

Both samplers run the SAME production code (agents/quant/goal.py, egoquant/regime) on the SAME real prices: a frozen
copy of EgoQuant's price cache dated 2026-10-18 (eval/regime/offline.py; no network, nothing written to the cache).

  A. Goal chances today: 8 fixed portfolios C1-C5 (bond-only to 90% stocks), 3 goals each, old vs new.
  B. Crash realism: max drawdown distribution and the share of 5-year paths with a fall of 10% or more that lasts
     more than 3 months, against every 5-year window of the same history; long crises in the regime chains against
     the labelled history.
  C. Calibration backtest: at each quarter-end 2012-06-30 .. 2021-06-30 both samplers predict 5-year (and, to
     2016-06-30, 10-year) goal chances from prices up to that day only; the regime model used at each date was FITTED
     ONLY ON DATA BEFORE IT (eval/results/regime_walkforward/, scripts/fit_regime_model.py --walk-forward). The
     portfolios are then replayed on the prices that really followed. Brier score, a calibration table and a
     date-bootstrap interval for the Brier difference.
  D. Runtime and peak memory of analyze_goal (20 portfolios, 15 / 30 / 60 years, 10,000 paths), current code and
     with the approved Z1 change (one wealth matrix at a time), each case in a fresh process.
  E. Limit checks (as ZC's Z3): /portfolio through the real HTTP layer for every demo persona x 3 goal settings with the
     flag on and off: every request answers, the Risk Engine never approves a portfolio that breaks a limit, and the
     What-If baseline equals the report's chance.

Acceptance criteria for recommending the flag: all tests pass (checked separately with pytest), Brier score no worse,
runtime +25% at most, peak memory not higher.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "eval" / "results"
WALK_FORWARD = RESULTS / "regime_walkforward"
PYTHON = sys.executable

PORTFOLIOS = {    # PROVISIONAL representative mixes inside each class's caps (R-levels and class caps of the C-level table)
    "P1 C1 cash and short Treasuries": {"BIL": 0.5, "SHY": 0.5},
    "P2 C2 bonds only (bond-heavy)": {"SHY": 0.2, "AGG": 0.4, "IEF": 0.25, "LQD": 0.15},
    "P3 C2 bonds with TIPS": {"AGG": 0.5, "TIP": 0.3, "BIL": 0.2},
    "P4 C3 30% stocks": {"VTI": 0.2, "EFA": 0.1, "AGG": 0.45, "IEF": 0.15, "GLD": 0.1},
    "P5 C3 40% stocks, long bonds": {"SPY": 0.4, "AGG": 0.3, "TLT": 0.2, "GLD": 0.1},
    "P6 C4 60/40": {"SPY": 0.45, "EFA": 0.15, "AGG": 0.3, "GLD": 0.1},
    "P7 C4 70% stocks (growth)": {"VTI": 0.5, "EFA": 0.1, "QQQ": 0.1, "AGG": 0.2, "GLD": 0.1},
    "P8 C5 90% stocks (equity-heavy)": {"VTI": 0.55, "QQQ": 0.15, "EFA": 0.1, "EEM": 0.1, "GLD": 0.1},
}
MARKET = "VT"
TICKERS = sorted({t for w in PORTFOLIOS.values() for t in w} | {MARKET})
START, MONTHLY = 100_000.0, 500.0
GOAL_MULTS_TODAY = (1.2, 1.5, 2.0)
GOAL_MULTS = {5: (1.05, 1.25, 1.5), 10: (1.1, 1.5, 2.0)}     # as ZC's Z2 calibration
HORIZON_TODAY = 15.0


# ==================================================================================================================
# Data (read-only, from the frozen cache copy)
# ==================================================================================================================
def load_prices(cache: Path, tickers=TICKERS):
    import pandas as pd
    series = {}
    for t in tickers:
        d = json.loads((cache / "tiingo" / f"{t}.json").read_text(encoding="utf-8"))
        s = pd.Series(d["prices"], dtype=float)
        s.index = pd.to_datetime(s.index)
        series[t] = s.sort_index()
    return pd.DataFrame(series).sort_index()


def catalog_info():
    from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG
    cat = {i.ticker: i for i in DEFAULT_CATALOG}
    return {t: cat[t].asset_class for t in TICKERS}, {t: float(cat[t].expense_ratio) for t in TICKERS}


def history(prices, classes, cfg):
    """The goal simulation's history, prepared exactly like Agent A's run_goal_based."""
    from egoquant.agents.quant import goal as gq
    daily, _, _ = gq.complete_history(prices, cfg.goal_block_days, cfg.goal_seed)
    daily, _, _ = gq.equilibrium_means(daily, classes, MARKET)
    return daily


def vec(weights: dict, columns) -> "np.ndarray":
    import numpy as np
    return np.array([weights.get(c, 0.0) for c in columns])


def spec(mult: float, years: float):
    from egoquant.agents.quant import goal as gq
    paid = START + MONTHLY * round(years * 12)
    return gq.GoalSpec(start_amount=START, monthly_flow=MONTHLY, goal_amount=mult * paid, horizon_years=years)


# ==================================================================================================================
# A. Goal chances today
# ==================================================================================================================
def section_a(prices, classes, fees, cfg, n_paths):
    from egoquant.agents.quant import goal as gq
    from egoquant.regime import hook
    from eval.regime.offline import AS_OF
    daily = history(prices.loc[:str(AS_OF)], classes, cfg)
    rows, record = [], None
    for name, w in PORTFOLIOS.items():
        for mult in GOAL_MULTS_TODAY:
            sp = spec(mult, HORIZON_TODAY)
            out = {"portfolio": name, "goal_multiple_of_paid_in": mult, "goal": sp.goal_amount, "years": sp.horizon_years}
            for label, sampler in (("old", None), ("new", "regime")):
                dec = hook.decide(daily.index, cfg.goal_block_days, AS_OF, "historical", forced=None) if sampler else None
                g = gq.analyze_goal(daily, [(0, name, 0.0, w)], fees, {0: 0.0}, sp, n_paths=n_paths,
                                    block_days=cfg.goal_block_days, seed=cfg.goal_seed, tie=cfg.goal_tie_tolerance,
                                    sampler=dec)
                o = g.outcomes[0]
                out[label] = {"prob_goal": o.prob_goal, "p10": o.p10, "p50": o.p50, "p90": o.p90,
                              "worst5_shortfall": o.worst5_shortfall, "drawdown_p90": o.drawdown_p90,
                              "prob_drawdown_20": o.prob_drawdown_20}
                if dec is not None:
                    record = g.sampler
                    out["new_sampler"] = g.sampler.get("sampler")
            out["change_points"] = round((out["new"]["prob_goal"] - out["old"]["prob_goal"]) * 100, 2)
            rows.append(out)
    return {"as_of": str(AS_OF), "history": [str(daily.index[0].date()), str(daily.index[-1].date())],
            "n_paths": n_paths, "start": START, "monthly": MONTHLY, "rows": rows,
            "regime_record": {k: v for k, v in (record or {}).items() if k != "block_transition"}}


# ==================================================================================================================
# B. Crash realism
# ==================================================================================================================
def _underwater_long(wealth, depth=0.10, months=3):
    """Per path: a fall of at least `depth` below a previous high that stays below that high for MORE than `months`."""
    import numpy as np
    peaks = np.maximum.accumulate(wealth, axis=1)
    under = wealth < peaks * (1 - 1e-12)
    deep = wealth <= peaks * (1 - depth)
    out = np.zeros(len(wealth), dtype=bool)
    run = np.zeros(len(wealth), dtype=np.int64)
    had_deep = np.zeros(len(wealth), dtype=bool)
    for m in range(wealth.shape[1]):
        run = np.where(under[:, m], run + 1, 0)
        had_deep = np.where(under[:, m], had_deep | deep[:, m], False)
        out |= (run > months) & had_deep
    return out


def _dd_stats(wealth):
    import numpy as np
    from egoquant.agents.quant import goal as gq
    dd = gq.max_drawdowns(wealth)
    return {"max_drawdown_p50": float(np.percentile(dd, 50)), "max_drawdown_p90": float(np.percentile(dd, 90)),
            "max_drawdown_p99": float(np.percentile(dd, 99)), "share_drawdown_20": float(np.mean(dd >= 0.20)),
            "share_fall_10_longer_than_3_months": float(np.mean(_underwater_long(wealth))), "n": int(len(dd))}


def _crisis_runs(chains_or_labels, blocks=3):
    import numpy as np
    c = chains_or_labels == 2
    n = c.shape[1]
    if n < blocks:
        return np.zeros(len(c), dtype=bool)
    run = c[:, : n - blocks + 1].copy()
    for k in range(1, blocks):
        run &= c[:, k: n - blocks + 1 + k]
    return run.any(axis=1)


def section_b(prices, classes, fees, cfg, n_paths):
    import numpy as np
    from egoquant.agents.quant import goal as gq
    from egoquant.regime import hook, load_regime_model
    from egoquant.regime.sampler import RegimeSampler
    from eval.regime.offline import AS_OF
    daily = history(prices.loc[:str(AS_OF)], classes, cfg)
    blocks = gq.block_returns(daily, cfg.goal_block_days)
    months = 60
    sp = gq.GoalSpec(start_amount=START, monthly_flow=0.0, goal_amount=START, horizon_years=5)
    fee_v = vec(fees, daily.columns)
    old = np.random.default_rng(cfg.goal_seed).integers(0, len(blocks), size=(n_paths, months))
    dec = hook.decide(daily.index, cfg.goal_block_days, AS_OF, "historical")
    new = dec.draws(len(blocks), n_paths, months, cfg.goal_seed)
    # the same chains again, kept (RegimeSampler.draws drops them) for the regime statistics
    chains = RegimeSampler(dec.sampler.current_pools(), dec.sampler.start_probs, dec.sampler.block_transition).chains(
        n_paths, months, np.random.default_rng(cfg.goal_seed))
    # history: every 5-year window starting every 21 trading days, on the same daily returns the simulation uses
    starts = list(range(0, len(blocks) - (months - 1) * cfg.goal_block_days, cfg.goal_block_days))
    hist = np.array([[s + m * cfg.goal_block_days for m in range(months)] for s in starts])
    model = load_regime_model()
    labels = model.labels_on(daily.index[: len(blocks)])
    hist_labels = labels[hist]
    labelled = (hist_labels >= 0).all(axis=1)
    out = {"months": months, "n_paths": n_paths, "n_history_windows": len(starts),
           "history_note": "history windows use the same daily returns as the simulation (hindsight-corrected "
                           "averages), consecutive 21-day months, monthly rebalancing and fees; they overlap heavily",
           "portfolios": {},
           "long_crisis": {"definition": "3 or more consecutive months starting in the crisis regime",
                           "regime_chains_share": float(_crisis_runs(chains).mean()),
                           "history_windows_share": float(_crisis_runs(hist_labels[labelled]).mean()),
                           "history_windows_labelled": int(labelled.sum()),
                           "start_regime_today": dec.record().get("regime_today")}}
    # the regime chains start from TODAY's regime (calm); for a like-for-like crisis frequency also start from the
    # stationary distribution (what a random historical window starts from)
    from egoquant.regime import stationary_distribution
    stat = RegimeSampler(dec.sampler.current_pools(), stationary_distribution(model.transition), dec.sampler.block_transition)
    out["long_crisis"]["regime_chains_share_from_stationary_start"] = float(
        _crisis_runs(stat.chains(n_paths, months, np.random.default_rng(cfg.goal_seed))).mean())
    stat_draws = stat.fill(stat.chains(n_paths, months, np.random.default_rng(cfg.goal_seed + 7)),
                           np.random.default_rng(cfg.goal_seed + 8))
    for name in ("P2 C2 bonds only (bond-heavy)", "P6 C4 60/40", "P8 C5 90% stocks (equity-heavy)"):
        w = vec(PORTFOLIOS[name], daily.columns)
        out["portfolios"][name] = {
            "old_block_bootstrap": _dd_stats(gq.simulate_wealth(blocks, w, fee_v, sp, old)),
            "new_regime_today_start": _dd_stats(gq.simulate_wealth(blocks, w, fee_v, sp, new)),
            "new_regime_stationary_start": _dd_stats(gq.simulate_wealth(blocks, w, fee_v, sp, stat_draws)),
            "history_5y_windows": _dd_stats(gq.simulate_wealth(blocks, w, fee_v, sp, hist)),
        }
    return out


# ==================================================================================================================
# C. Calibration backtest (walk-forward models: no look-ahead in the regime model)
# ==================================================================================================================
def section_c(prices, classes, fees, cfg, n_paths, quick=False):
    import numpy as np
    import pandas as pd
    from egoquant.agents.quant import goal as gq
    from egoquant.regime import hook
    returns_all = prices.pct_change(fill_method=None)
    rows, dates_info = [], []
    dates = list(pd.date_range("2012-06-30", "2021-06-30", freq="QE"))
    if quick:
        dates = dates[::6]
    for t in dates:
        model_path = WALK_FORWARD / f"regime_hmm_v1_asof_{t.date()}.json"
        past = prices.loc[:t]
        daily = history(past, classes, cfg)
        info = {"date": str(t.date()), "history_days": len(daily), "model": model_path.name}
        if len(daily) < cfg.goal_min_history_days:
            info["skipped"] = "less than the minimum history (no chance shown by either sampler)"
            dates_info.append(info)
            continue
        blocks = gq.block_returns(daily, cfg.goal_block_days)
        future = returns_all.loc[returns_all.index > t]           # the trading days AFTER t (t may be a weekend)
        fee_v = vec(fees, daily.columns)
        for years, mults in GOAL_MULTS.items():
            months = years * 12
            if len(future) < months * cfg.goal_block_days or (years == 10 and t > pd.Timestamp("2016-06-30")):
                continue
            # realised: consecutive 21-day months of what really happened (raw returns), same rules as the simulation
            fut = future.iloc[: months * cfg.goal_block_days][list(daily.columns)].fillna(0.0)
            real_blocks = np.exp(np.log1p(fut.to_numpy()).reshape(months, cfg.goal_block_days, -1).sum(axis=1))
            real_draws = np.arange(months)[None, :]
            old = np.random.default_rng(cfg.goal_seed).integers(0, len(blocks), size=(n_paths, months))
            dec = hook.decide(daily.index, cfg.goal_block_days, t.date(), "historical", model_path=model_path)
            new = dec.draws(len(blocks), n_paths, months, cfg.goal_seed)
            rec = dec.record()
            info.setdefault("sampler", {})[f"{years}y"] = rec.get("sampler")
            info.setdefault("fallback_reason", {})[f"{years}y"] = rec.get("fallback_reason")
            info["regime_at_start"] = rec.get("regime_today")
            info["regime_prob_at_start"] = rec.get("regime_today_probability")
            for name, wd in PORTFOLIOS.items():
                w = vec(wd, daily.columns)
                sp = spec(1.0, years)
                f_old = gq.simulate_wealth(blocks, w, fee_v, sp, old)[:, -1]
                f_new = gq.simulate_wealth(blocks, w, fee_v, sp, new)[:, -1] if new is not None else f_old
                f_real = float(gq.simulate_wealth(real_blocks, w, fee_v, sp, real_draws)[0, -1])
                for mult in mults:
                    goal = mult * (START + MONTHLY * months)
                    rows.append({"date": str(t.date()), "years": years, "portfolio": name, "goal_multiple": mult,
                                 "pred_old": float(np.mean(f_old >= goal)), "pred_new": float(np.mean(f_new >= goal)),
                                 "outcome": int(f_real >= goal), "realised_final": round(f_real, 2),
                                 "new_sampler": rec.get("sampler")})
        dates_info.append(info)
    return {"rows": rows, "dates": dates_info, "metrics": calibration_metrics(rows)}


def _wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def calibration_metrics(rows):
    import numpy as np
    out = {}
    for years in sorted({r["years"] for r in rows}):
        sub = [r for r in rows if r["years"] == years]
        o = np.array([r["outcome"] for r in sub], dtype=float)
        res = {"cases": len(sub), "start_dates": len({r["date"] for r in sub}), "share_reached": float(o.mean()),
               "brier_base_rate": float(np.mean((o.mean() - o) ** 2))}
        for key in ("old", "new"):
            p = np.array([r[f"pred_{key}"] for r in sub])
            res[f"mean_pred_{key}"] = float(p.mean())
            res[f"brier_{key}"] = float(np.mean((p - o) ** 2))
            table = []
            for lo in np.arange(0, 1.0, 0.1):
                hi = lo + 0.1
                m = (p >= lo) & ((p < hi) if hi < 0.999 else (p <= 1.0))
                k, n = int(o[m].sum()), int(m.sum())
                ci = _wilson(k, n)
                table.append({"bin": f"{lo:.0%}-{hi:.0%}", "cases": n, "mean_pred": float(p[m].mean()) if n else None,
                              "reached": k / n if n else None, "ci95": ci})
            res[f"calibration_{key}"] = table
        res["brier_skill_old"] = 1 - res["brier_old"] / res["brier_base_rate"] if res["brier_base_rate"] else None
        res["brier_skill_new"] = 1 - res["brier_new"] / res["brier_base_rate"] if res["brier_base_rate"] else None
        # outcomes on the same start date are strongly correlated: resample whole start dates
        by_date = {}
        for r in sub:
            by_date.setdefault(r["date"], []).append(((r["pred_new"] - r["outcome"]) ** 2) - ((r["pred_old"] - r["outcome"]) ** 2))
        keys = sorted(by_date)
        rng = np.random.default_rng(20261017)
        diffs = []
        for _ in range(4000):
            pick = rng.integers(0, len(keys), len(keys))
            vals = [v for i in pick for v in by_date[keys[i]]]
            diffs.append(float(np.mean(vals)))
        res["brier_new_minus_old"] = res["brier_new"] - res["brier_old"]
        res["brier_diff_ci95_date_bootstrap"] = [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))]
        per = {}
        for name in PORTFOLIOS:
            s2 = [r for r in sub if r["portfolio"] == name]
            o2 = np.array([r["outcome"] for r in s2], dtype=float)
            per[name] = {"cases": len(s2), "reached": float(o2.mean()),
                         "mean_pred_old": float(np.mean([r["pred_old"] for r in s2])),
                         "mean_pred_new": float(np.mean([r["pred_new"] for r in s2])),
                         "brier_old": float(np.mean([(r["pred_old"] - r["outcome"]) ** 2 for r in s2])),
                         "brier_new": float(np.mean([(r["pred_new"] - r["outcome"]) ** 2 for r in s2]))}
        res["per_portfolio"] = per
        out[f"{years}y"] = res
    return out


# ==================================================================================================================
# D. Runtime and peak memory (each case in a fresh process)
# ==================================================================================================================
def shortlist(n=20):
    """20 distinct portfolios between the bond-only and the 90% stock mix (Agent B keeps up to 20)."""
    a, b = PORTFOLIOS["P2 C2 bonds only (bond-heavy)"], PORTFOLIOS["P8 C5 90% stocks (equity-heavy)"]
    out = []
    for k in range(n):
        x = k / (n - 1)
        w = {t: (1 - x) * a.get(t, 0.0) + x * b.get(t, 0.0) for t in set(a) | set(b)}
        out.append((k, "mix", x, w))
    return out


def analyze_goal_lean(daily, candidates, annual_fees, vols, spec_, *, n_paths, block_days, seed, tie, sampler=None):
    """ZC's approved Z1 change (eval/zc/z1_lean_goal.py) plus the sampler hook: one wealth matrix at a time, the chosen
    portfolio re-simulated after ranking. Measurement only; Agent A's goal.py is not changed by this task."""
    import numpy as np
    from egoquant.agents.quant import goal as gq
    assets = list(daily.columns)
    blocks = gq.block_returns(daily, block_days)
    draws = sampler.draws(len(blocks), n_paths, spec_.months, seed) if sampler is not None else None
    if draws is None:
        draws = np.random.default_rng(seed).integers(0, len(blocks), size=(n_paths, spec_.months))
    fees = np.array([annual_fees.get(a, 0.0) for a in assets])
    outcomes, weights_by = [], {}
    for menu_index, style, equity, weights in candidates:
        w = np.array([weights.get(a, 0.0) for a in assets])
        wealth = gq.simulate_wealth(blocks, w, fees, spec_, draws)
        weights_by[menu_index] = w
        outcomes.append(gq.outcome(menu_index, style, equity, wealth[:, -1], (wealth[:, 1:] <= 0).any(axis=1),
                                   spec_.goal_amount, gq.max_drawdowns(wealth)))
        del wealth
    ranked = gq.rank(outcomes, vols, tie)
    w = weights_by[ranked[0].menu_index]
    wealth = gq.simulate_wealth(blocks, w, fees, spec_, draws)
    levers = gq.goal_levers(blocks, w, fees, spec_, draws, wealth[:, -1], seed,
                            extend=sampler.extend if sampler is not None else None)
    checks = gq.period_checks(daily, blocks, w, fees, spec_, n_paths, block_days, seed, sampler)
    return ranked, levers, checks


def bench_case(years: float, sampler: str, variant: str, reps: int) -> dict:
    """One measurement (run in its own process): median time of `reps` runs after a warm-up, tracemalloc peak and the
    process's peak working set."""
    import tracemalloc
    from eval.regime.offline import AS_OF, install, process_memory_mb
    cache = install()                                             # before egoquant is imported
    from egoquant.agents.quant import goal as gq
    from egoquant.models import AgentConfig
    from egoquant.regime import hook
    cfg = AgentConfig()
    classes, fees = catalog_info()
    daily = history(load_prices(cache).loc[:str(AS_OF)], classes, cfg)
    cands = shortlist()
    sp = spec(1.5, years)

    def once():
        dec = hook.decide(daily.index, cfg.goal_block_days, AS_OF, "historical") if sampler == "new" else None
        kw = dict(n_paths=cfg.goal_paths, block_days=cfg.goal_block_days, seed=cfg.goal_seed, tie=cfg.goal_tie_tolerance)
        if variant == "current":
            return gq.analyze_goal(daily, cands, fees, {c[0]: c[2] for c in cands}, sp, sampler=dec, **kw)
        return analyze_goal_lean(daily, cands, fees, {c[0]: c[2] for c in cands}, sp, sampler=dec, **kw)

    os.environ["EGOQUANT_REGIME_MC"] = "1" if sampler == "new" else "0"
    once()                                                        # warm-up (model load, caches)
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        once()
        times.append(time.perf_counter() - t0)
    ws_before, _ = process_memory_mb()
    tracemalloc.start()
    once()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    _, ws_peak = process_memory_mb()
    return {"years": years, "sampler": sampler, "variant": variant, "time_s_median": statistics.median(times),
            "times_s": times, "tracemalloc_peak_mb": peak / 2**20, "process_peak_working_set_mb": ws_peak,
            "working_set_before_mb": ws_before, "n_portfolios": len(cands), "n_paths": cfg.goal_paths}


def section_d(reps: int, horizons=(15.0, 30.0, 60.0)):
    out = []
    for years in horizons:
        for variant in ("current", "z1_lean"):
            for sampler in ("old", "new"):
                proc = subprocess.run([PYTHON, str(Path(__file__)), "--bench", str(years), sampler, variant, str(reps)],
                                      capture_output=True, text=True, cwd=str(ROOT))
                line = [ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH ")]
                if proc.returncode != 0 or not line:
                    out.append({"years": years, "sampler": sampler, "variant": variant, "error": proc.stderr[-800:]})
                    continue
                out.append(json.loads(line[-1][6:]))
                print(f"  bench {years:>4} y {variant:<8} {sampler}: {out[-1]['time_s_median']:.2f} s, "
                      f"tracemalloc {out[-1]['tracemalloc_peak_mb']:.0f} MB, peak WS "
                      f"{out[-1]['process_peak_working_set_mb']:.0f} MB", flush=True)
    return out


# ==================================================================================================================
# E. Limit checks through the HTTP layer (own process: the offline route harness patches modules)
# ==================================================================================================================
def limits_case() -> dict:
    from eval.regime.offline import install_routes
    install_routes()
    from fastapi.testclient import TestClient
    from main import app
    from egoquant.agents import what_if as wi
    from egoquant.demo.goal_walkthrough import default_goal
    from egoquant.demo.personas import PERSONAS
    from egoquant.report import _in_100
    client = TestClient(app)
    runs = []
    for persona in sorted(PERSONAS):
        p = PERSONAS[persona]
        base = {"answers": {"choices": p.choices, "multi_choices": {"K4": p.k4}, "amount_invested": p.amount_invested,
                            "amount_needed": p.amount_needed},
                "preferences": {"preferences_version": p.preference_answers().preferences_version,
                                "choices": p.preferences},
                "trading_platforms": p.trading_platforms, "horizon_years": p.horizon_years,
                "liquidity_need": p.liquidity_need, "min_cash_buffer": p.min_cash_buffer}
        demo = default_goal(persona)
        hard = 2.2 * (demo.start_amount + demo.monthly_flow * demo.months)    # Z3's "hard goal"
        goals = {"none": None, "demo": demo, "hard_2.2x": demo.model_copy(update={"goal_amount": hard})}
        for gname, g in goals.items():
            body = dict(base)
            if g is not None:
                body.update(goal_amount=g.goal_amount, monthly_flow=g.monthly_flow, horizon_years=g.horizon_years)
            for flag in ("0", "1"):
                os.environ["EGOQUANT_REGIME_MC"] = flag
                wi._rate_hits.clear()
                t0 = time.perf_counter()
                r = client.post("/portfolio", json=body)
                secs = time.perf_counter() - t0
                row = {"persona": persona, "goal": gname, "flag": flag, "status": r.status_code, "seconds": round(secs, 2)}
                if r.status_code == 200:
                    out = r.json()
                    rep = out.get("report") or {}
                    checks = out.get("checks") or []
                    limits = rep.get("limits") or []
                    approved = bool(rep.get("risk_review_approved"))
                    row.update(approved=approved, n_checks=len(checks),
                               checks_failed=[c.get("name") for c in checks if not c.get("passed", c.get("ok", True))],
                               limits_broken=[lc.get("name") for lc in limits if lc.get("ok") is False],
                               weights={a["asset_id"]: a["weight"] for a in rep.get("allocation") or []},
                               engine_version=rep.get("engine_version"), input_hash=rep.get("input_hash"),
                               sampler=((out.get("goal") or {}).get("sampler") or {}).get("sampler"),
                               fallback_reason=((out.get("goal") or {}).get("sampler") or {}).get("fallback_reason"),
                               prob_goal=((out.get("goal") or {}).get("chosen") or {}).get("prob_goal"),
                               goal_unavailable=out.get("goal_unavailable"))
                    row["risk_engine_consistent"] = (not approved) or (not row["checks_failed"] and not row["limits_broken"])
                    if row["prob_goal"] is not None and out.get("what_if_context"):
                        w = wi.run_what_if(out["what_if_context"], {"extra_monthly_contribution": 100}, timeout_s=120)
                        row["what_if_status"] = w.status
                        row["what_if_baseline_equals_report"] = (w.baseline_goal_frequency == _in_100(row["prob_goal"]))
                runs.append(row)
                print(f"  {persona:<20} {gname:<10} flag {flag}: {row['status']} {row.get('approved')} "
                      f"{row.get('sampler')} p={row.get('prob_goal')} {row['seconds']} s", flush=True)
    return {"runs": runs}


def section_e():
    proc = subprocess.run([PYTHON, str(Path(__file__)), "--limits"], capture_output=True, text=True, cwd=str(ROOT))
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LIMITS ")]
    if proc.returncode != 0 or not line:
        return {"error": proc.stderr[-2000:]}
    res = json.loads(line[-1][7:])
    runs = res["runs"]
    by = {}
    for r in runs:
        by.setdefault((r["persona"], r["goal"]), {})[r["flag"]] = r
    changed = [k for k, v in by.items() if "0" in v and "1" in v and v["0"].get("weights") != v["1"].get("weights")]
    res["summary"] = {
        "requests": len(runs), "all_answered_200": all(r["status"] == 200 for r in runs),
        "approved": sum(1 for r in runs if r.get("approved")),
        "risk_engine_inconsistent": [f"{r['persona']}/{r['goal']}/flag{r['flag']}" for r in runs
                                     if r.get("risk_engine_consistent") is False],
        "limits_broken_in_approved": [f"{r['persona']}/{r['goal']}/flag{r['flag']}" for r in runs
                                      if r.get("approved") and r.get("limits_broken")],
        "chosen_portfolio_changed_by_flag": [f"{p}/{g}" for p, g in changed],
        "what_if_baseline_mismatches": [f"{r['persona']}/{r['goal']}/flag{r['flag']}" for r in runs
                                        if r.get("what_if_baseline_equals_report") is False],
        "regime_runs": sum(1 for r in runs if str(r.get("sampler") or "").startswith("regime_hmm_")),
        "fallbacks_with_flag_on": sorted({f"{r['persona']}/{r['goal']}: {r.get('fallback_reason')}" for r in runs
                                          if r["flag"] == "1" and r.get("fallback_reason")}),
        "median_seconds_flag_off": statistics.median([r["seconds"] for r in runs if r["flag"] == "0"]),
        "median_seconds_flag_on": statistics.median([r["seconds"] for r in runs if r["flag"] == "1"]),
    }
    return res


# ==================================================================================================================
# Summary
# ==================================================================================================================
def acceptance(c, d):
    crit = {}
    briers = [(k, v["brier_old"], v["brier_new"]) for k, v in c["metrics"].items()]
    crit["brier_not_worse"] = {"pass": all(new <= old + 1e-12 for _, old, new in briers),
                               "detail": {k: {"old": old, "new": new} for k, old, new in briers}}
    pairs = {}
    for r in d:
        if "error" not in r:
            pairs.setdefault((r["years"], r["variant"]), {})[r["sampler"]] = r
    rt, mem = {}, {}
    for (years, variant), v in sorted(pairs.items()):
        if "old" in v and "new" in v:
            rt[f"{years:g}y {variant}"] = v["new"]["time_s_median"] / v["old"]["time_s_median"] - 1
            mem[f"{years:g}y {variant}"] = {"tracemalloc_mb": [v["old"]["tracemalloc_peak_mb"], v["new"]["tracemalloc_peak_mb"]],
                                            "peak_ws_mb": [v["old"]["process_peak_working_set_mb"],
                                                           v["new"]["process_peak_working_set_mb"]]}
    crit["runtime_increase_below_25pct"] = {"pass": bool(rt) and all(x < 0.25 for x in rt.values()), "detail": rt}
    for m in mem.values():
        m["tracemalloc_increase_mb"] = m["tracemalloc_mb"][1] - m["tracemalloc_mb"][0]
    crit["peak_memory_not_higher"] = {
        "pass": bool(mem) and all(m["tracemalloc_increase_mb"] <= 0 for m in mem.values()),
        "rule": "tracemalloc peak of analyze_goal (exact; numpy allocations included): new <= old, no slack",
        "max_increase_mb": max((m["tracemalloc_increase_mb"] for m in mem.values()), default=None),
        "detail": mem}
    return crit


def write_markdown(res, path: Path):
    a, b, c, d, e, acc = res["A"], res["B"], res["C"], res["D"], res["E"], res["acceptance"]
    L = [f"# Regime-aware sampler vs block bootstrap ({res['created']})", "",
         f"Commit `{res['commit']}`; frozen real-price cache dated 2026-10-18 (offline); model `{res['model_sha256'][:16]}`.",
         ""]
    L += ["## A. Goal chances today (15 years, $100,000 start, +$500/month; 10,000 paths)", "",
          f"Regime today: **{a['regime_record'].get('regime_today')}** "
          f"(probability {a['regime_record'].get('regime_today_probability', 0):.1%}).", "",
          "| Portfolio | Goal (x paid in) | Old chance | New chance | Change (points) |", "|---|---|---|---|---|"]
    for r in a["rows"]:
        L.append(f"| {r['portfolio']} | {r['goal_multiple_of_paid_in']:g}x (${r['goal']:,.0f}) | {r['old']['prob_goal']:.1%} | "
                 f"{r['new']['prob_goal']:.1%} | {r['change_points']:+.1f} |")
    L += ["", "## B. Crash realism (5-year paths, no contributions)", "",
          "| Portfolio | Sampler | Max drawdown p50 / p90 / p99 | Fall >= 20% | Fall >= 10% lasting > 3 months |",
          "|---|---|---|---|---|"]
    for name, v in b["portfolios"].items():
        for key, label in (("old_block_bootstrap", "old"), ("new_regime_today_start", "new (today: calm)"),
                           ("new_regime_stationary_start", "new (stationary start)"), ("history_5y_windows", "history")):
            s = v[key]
            L.append(f"| {name} | {label} | {s['max_drawdown_p50']:.1%} / {s['max_drawdown_p90']:.1%} / "
                     f"{s['max_drawdown_p99']:.1%} | {s['share_drawdown_20']:.1%} | {s['share_fall_10_longer_than_3_months']:.1%} |")
    lc = b["long_crisis"]
    L += ["", f"Long crises (3+ consecutive crisis months) in 5 years: regime chains {lc['regime_chains_share']:.1%} "
              f"(starting from today's calm regime), {lc['regime_chains_share_from_stationary_start']:.1%} from the "
              f"stationary distribution; history {lc['history_windows_share']:.1%} of {lc['history_windows_labelled']} "
              f"overlapping 5-year windows. {b['history_note']}.", ""]
    L += ["## C. Calibration backtest (walk-forward regime models; real outcomes)", ""]
    for k, m in c["metrics"].items():
        L += [f"**{k} goals:** {m['cases']} cases, {m['start_dates']} start dates, reached {m['share_reached']:.0%}.", "",
              "| | Mean predicted | Brier | Brier skill vs base rate |", "|---|---|---|---|",
              f"| Old (block bootstrap) | {m['mean_pred_old']:.1%} | {m['brier_old']:.4f} | {m['brier_skill_old']:+.1%} |",
              f"| New (regime) | {m['mean_pred_new']:.1%} | {m['brier_new']:.4f} | {m['brier_skill_new']:+.1%} |",
              f"| Base rate only | {m['share_reached']:.1%} | {m['brier_base_rate']:.4f} | 0 |", "",
              f"Brier new - old: {m['brier_new_minus_old']:+.4f} (95% interval resampling start dates: "
              f"{m['brier_diff_ci95_date_bootstrap'][0]:+.4f} to {m['brier_diff_ci95_date_bootstrap'][1]:+.4f}).", "",
              "| Portfolio | Reached | Mean pred old | Mean pred new | Brier old | Brier new |", "|---|---|---|---|---|---|"]
        for name, p in m["per_portfolio"].items():
            L.append(f"| {name} | {p['reached']:.0%} | {p['mean_pred_old']:.0%} | {p['mean_pred_new']:.0%} | "
                     f"{p['brier_old']:.3f} | {p['brier_new']:.3f} |")
        L += ["", "| Predicted | Old: cases / reached | New: cases / reached |", "|---|---|---|"]
        for o, n in zip(m["calibration_old"], m["calibration_new"]):
            fo = f"{o['cases']} / {o['reached']:.0%}" if o["cases"] else "0 / -"
            fn = f"{n['cases']} / {n['reached']:.0%}" if n["cases"] else "0 / -"
            L.append(f"| {o['bin']} | {fo} | {fn} |")
        L.append("")
    fb = sorted({f"{x['date']}: {x.get('fallback_reason')}" for x in c["dates"]
                 if any((x.get("fallback_reason") or {}).values())})
    if fb:
        L += ["Dates where the regime sampler fell back (both columns then equal): " + "; ".join(fb), ""]
    L += ["## D. Runtime and peak memory of analyze_goal (20 portfolios, 10,000 paths)", "",
          "| Horizon | Code | Old time | New time | Change | tracemalloc peak old / new | Process peak WS old / new |",
          "|---|---|---|---|---|---|---|"]
    pairs = {}
    for r in d:
        if "error" not in r:
            pairs.setdefault((r["years"], r["variant"]), {})[r["sampler"]] = r
    for (years, variant), v in sorted(pairs.items()):
        if "old" in v and "new" in v:
            o, n = v["old"], v["new"]
            L.append(f"| {years:g} y | {variant} | {o['time_s_median']:.2f} s | {n['time_s_median']:.2f} s | "
                     f"{n['time_s_median'] / o['time_s_median'] - 1:+.0%} | {o['tracemalloc_peak_mb']:.2f} / "
                     f"{n['tracemalloc_peak_mb']:.2f} MB | {o['process_peak_working_set_mb']:.0f} / "
                     f"{n['process_peak_working_set_mb']:.0f} MB |")
    s = e.get("summary") or {}
    L += ["", "## E. Limit checks (/portfolio through the HTTP layer, every persona x 3 goals x flag off/on)", ""]
    if s:
        L += [f"- {s['requests']} requests, all answered 200: **{s['all_answered_200']}**; approved: {s['approved']}.",
              f"- Risk Engine approved a portfolio with a failed check or broken limit: "
              f"**{len(s['risk_engine_inconsistent']) + len(s['limits_broken_in_approved'])}** cases.",
              f"- Regime sampler ran in {s['regime_runs']} requests; fallbacks with the flag on: "
              f"{', '.join(s['fallbacks_with_flag_on']) or 'none'}.",
              f"- Chosen portfolio changed by the flag: {', '.join(s['chosen_portfolio_changed_by_flag']) or 'none'}.",
              f"- What-If baseline different from the report's chance: {len(s['what_if_baseline_mismatches'])}.",
              f"- Median request time: flag off {s['median_seconds_flag_off']:.1f} s, flag on {s['median_seconds_flag_on']:.1f} s.", ""]
    else:
        L += [f"Error: {e.get('error', '')[:500]}", ""]
    L += ["## Acceptance criteria", ""]
    for k, v in acc.items():
        extra = f" (largest increase {v['max_increase_mb'] * 1024:+.0f} KiB)" if v.get("max_increase_mb") is not None else ""
        L.append(f"- {k}: **{'PASS' if v['pass'] else 'FAIL'}**{extra}")
    L += ["- all tests pass: checked separately (pytest), see the PR description.", "",
          f"**Recommendation: {res['recommendation']}**", ""]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--bench", nargs=4, metavar=("YEARS", "SAMPLER", "VARIANT", "REPS"))
    ap.add_argument("--limits", action="store_true")
    ap.add_argument("--skip", default="", help="comma-separated sections to skip (e.g. D,E)")
    args = ap.parse_args(argv)

    if args.bench:
        years, sampler, variant, reps = args.bench
        print("BENCH " + json.dumps(bench_case(float(years), sampler, variant, int(reps))))
        return 0
    if args.limits:
        print("LIMITS " + json.dumps(limits_case()))
        return 0

    from eval.regime import offline
    cache = offline.install(regime_flag="1")
    from egoquant.models import AgentConfig
    from egoquant.regime import load_regime_model
    cfg = AgentConfig()
    n_paths = 2_000 if args.quick else cfg.goal_paths
    classes, fees = catalog_info()
    prices = load_prices(cache)
    skip = {s.strip().upper() for s in args.skip.split(",") if s.strip()}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    res = {"created": datetime.now().strftime("%Y-%m-%d %H:%M"), "commit": commit, "quick": args.quick,
           "model_sha256": load_regime_model().sha256, "portfolios": PORTFOLIOS}
    t0 = time.perf_counter()
    print("A. goal chances today", flush=True)
    res["A"] = section_a(prices, classes, fees, cfg, n_paths)
    print("B. crash realism", flush=True)
    res["B"] = section_b(prices, classes, fees, cfg, n_paths)
    print("C. calibration backtest", flush=True)
    res["C"] = section_c(prices, classes, fees, cfg, n_paths, quick=args.quick)
    print("D. runtime and memory", flush=True)
    res["D"] = [] if "D" in skip else section_d(reps=1 if args.quick else 3,
                                                horizons=(15.0,) if args.quick else (15.0, 30.0, 60.0))
    print("E. limit checks", flush=True)
    res["E"] = {"skipped": True} if "E" in skip else section_e()
    res["acceptance"] = acceptance(res["C"], res["D"])
    e_ok = (res["E"].get("summary") or {})
    e_pass = bool(e_ok) and e_ok["all_answered_200"] and not e_ok["risk_engine_inconsistent"] \
        and not e_ok["limits_broken_in_approved"] and not e_ok["what_if_baseline_mismatches"]
    res["acceptance"]["limit_checks_and_what_if"] = {"pass": e_pass}
    all_pass = all(v["pass"] for v in res["acceptance"].values())
    res["recommendation"] = ("turn the flag on (all measured criteria met; confirm the test suite)" if all_pass and not args.quick
                             else "keep EGOQUANT_REGIME_MC off (see the failed criteria)" if not all_pass
                             else "quick run only: not a basis for the decision")
    res["seconds"] = round(time.perf_counter() - t0, 1)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = "_quick" if args.quick else ""
    out_json = RESULTS / f"regime_compare_{stamp}{tag}.json"
    out_md = RESULTS / f"regime_summary_{stamp}{tag}.md"
    out_json.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    write_markdown(res, out_md)
    offline.cleanup()
    print(f"written {out_json.name} and {out_md.name} in {res['seconds']} s")
    print(out_md.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
