"""Z2: Monte Carlo calibration backtest. Can we trust "a 69% chance of reaching your goal"?

At past decision dates t (quarter ends), the PRODUCTION goal path (Agent C -> Agent A's 100 candidates -> Agent B's
shortlist -> 10,000-path block bootstrap -> highest chance) runs on prices up to t ONLY. The chosen portfolio is then
replayed on the prices that actually followed, with the simulation's own rules (21-trading-day months, rebalanced
monthly, fund fees, the monthly contribution). Each run gives a predicted chance and a real yes/no, plus where the
real final amount fell among the predicted percentiles (p10/p25/p50/p75/p90).

No look-ahead:
  * prices: CutoffCacheSource serves the frozen cache only up to t (hard cutoff, independent of what Agent C asks);
    a fund whose first real price is after t does not exist at t;
  * universe: Agent C's CURATED catalog of broad funds, interests "stocks, bonds, gold" (C2: "bonds, cash"); not the
    directory's most-traded list or single stocks (today's winners: survivorship in the REAL outcome), no crypto
    (Binance history starts 2017);
  * the replay uses the prices after t of exactly the chosen weights and fees, with no re-optimization.

Runs:
  main         production settings unchanged; t = quarter ends with >= 5 years of history before t and the whole
               horizon before the last cached close; 5-year goals (and 10-year goals where they fit)
  sensitivity  NOT THE PRODUCTION MODEL: goal_min_history_days lowered to 1 year so 2008-2011 starts are included

  anchor_tpX   EXPERIMENT, NOT THE PRODUCTION MODEL (Z2 finding 1): the main run with bond and cash AVERAGE returns
               anchored to the rate at t instead of their sample average: cash = BIL's trailing 63-day return at t
               (annualized), bonds = that rate + a term premium X (assumption; tested at several X). Daily moves are
               kept; only each bond/cash column's mean is shifted. Applied just before the simulation.

Cached prices start 2007-05-23, so at these dates the simulation saw 5-14 years of history (production: ~20).

    python eval/zc/z2_calibration.py [--workers 10] [--only main|sensitivity] [--limit N] [--anchor-tp 0.005 0.01]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import offline  # noqa: E402

TYPES = {  # investor type -> (persona supplying the questionnaire answers, interest areas)
    "C2": ("cautious_retiree", ["bonds", "cash"]),
    "C3": ("balanced_saver", ["stocks", "bonds", "gold"]),
    "C4": ("growth_investor", ["stocks", "bonds", "gold"]),
    "C5": ("crypto_trader", ["stocks", "bonds", "gold"]),
}
START, MONTHLY = 100_000.0, 500.0
GOAL_MULTIPLES = {5: (1.05, 1.25, 1.5), 10: (1.1, 1.5, 2.0)}   # x the money paid in: a spread of chances
LAST_CLOSE = date(2026, 10, 16)
BLOCK = 21
PCTS = ("p10", "p25", "p50", "p75", "p90")


def quarter_ends(first: date, last: date) -> list[date]:
    out, y = [], first.year
    while True:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, d)
            if first <= q <= last:
                out.append(q)
            elif q > last:
                return out
        y += 1


# --------------------------------------------------------------------------------------------------------------
# worker side
# --------------------------------------------------------------------------------------------------------------
_PRICES: dict = {}
_ANCHOR: dict = {"tp": None, "t": None, "short_rate": None}


def short_rate_at(t: date) -> float:
    """BIL's trailing 63-trading-day return up to t, annualized: the cash rate the market paid at t."""
    import pandas as pd
    s = _series("BIL").loc[: pd.Timestamp(t)].dropna()
    if len(s) < 64:
        raise ValueError("not enough BIL history for the short rate")
    return float((s.iloc[-1] / s.iloc[-64]) ** (252 / 63) - 1)


def anchor_means(daily, t: date, tp: float):
    """Shift each bond / cash column so its mean daily return matches the anchored annual rate (moves kept)."""
    from egoquant.agents.market_intel import DEFAULT_CATALOG
    classes = {i.ticker: i.asset_class for i in DEFAULT_CATALOG}
    rate = short_rate_at(t)
    _ANCHOR["short_rate"] = rate
    out = daily.copy()
    for col in out.columns:
        cls = classes.get(col)
        if cls in ("cash", "bond"):
            annual = rate if cls == "cash" else rate + tp
            target = (1 + annual) ** (1 / 252) - 1
            out[col] = out[col] - out[col].mean() + target
    return out


def _install_anchor():
    from egoquant.agents.quant import goal as gq
    original = gq.analyze_goal

    def analyze_goal_anchored(daily, *a, **k):
        if _ANCHOR["tp"] is not None:
            daily = anchor_means(daily, _ANCHOR["t"], _ANCHOR["tp"])
        return original(daily, *a, **k)

    gq.analyze_goal = analyze_goal_anchored


def _init_worker():
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    offline.install()
    _install_anchor()                 # inactive unless a job sets anchor_tp


def _series(ticker: str):
    import pandas as pd
    if ticker not in _PRICES:
        path = offline.FROZEN_CACHE / "tiingo" / f"{ticker.upper()}.json"
        if not path.exists():
            _PRICES[ticker] = None
        else:
            prices = json.loads(path.read_text(encoding="utf-8"))["prices"]
            s = pd.Series(prices, dtype=float)
            s.index = pd.to_datetime(s.index)
            _PRICES[ticker] = s.sort_index()
    return _PRICES[ticker]


class CutoffCacheSource:
    """The frozen cache, never past `cutoff` (point in time), never a request."""

    name, data_source = "tiingo", "historical"

    def __init__(self, cutoff: date):
        self.cutoff = cutoff

    def fetch_adjusted_close(self, tickers, start, end):
        import pandas as pd
        last = min(end, self.cutoff)
        cols = {}
        for t in tickers:
            s = _series(t)
            if s is not None:
                part = s.loc[pd.Timestamp(start):pd.Timestamp(last)]
                if not part.empty:
                    cols[t] = part
        return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame(index=pd.DatetimeIndex([]))

    def needs_request(self, ticker, start, end):
        return False

    def allows_new(self, ticker):
        return True


def realized_final(weights: dict[str, float], fees: dict[str, float], t: date, months: int, flow: float,
                   start_amount: float) -> tuple[float | None, str]:
    """Replay the chosen weights on the prices AFTER t: the simulation's rules, real blocks in order."""
    import numpy as np
    import pandas as pd
    held = [a for a, w in weights.items() if w > 1e-9]
    frame = pd.DataFrame({a: _series(a) for a in held})
    frame = frame.loc[frame.index > pd.Timestamp(t)].ffill().dropna()
    need = months * BLOCK                                       # closes after t: month m ends at m*21-1
    if len(frame) < need:
        return None, f"only {len(frame)} closes after t, need {need}"
    w = np.array([weights[a] for a in held])
    fee = float(w @ np.array([fees.get(a, 0.0) for a in held]))
    px = frame.to_numpy()
    # Base = the last close at or before t, so month 1 includes the first day after t (as a simulated block does).
    base_frame = pd.DataFrame({a: _series(a) for a in held})
    base_frame = base_frame.loc[base_frame.index <= pd.Timestamp(t)].ffill().dropna()
    if base_frame.empty:
        return None, "no close at t"
    base = base_frame.to_numpy()[-1]
    wealth = start_amount
    prev = base
    for m in range(months):
        end = px[(m + 1) * BLOCK - 1]
        gross = end / prev
        wealth = max(wealth * float(w @ gross) * (1.0 - fee / 12.0) + flow, 0.0)
        prev = end
    return wealth, ""


def run_one(job: dict) -> dict:
    t = date.fromisoformat(job["t"])
    row = {k: job[k] for k in ("run", "t", "type", "horizon", "multiple")}
    _ANCHOR.update(tp=job.get("anchor_tp"), t=t, short_rate=None)
    try:
        from egoquant.agents.quant.goal import GoalSpec
        from egoquant.demo.personas import PERSONAS
        from egoquant.models import AgentConfig
        from egoquant.pipeline import run_pipeline

        persona_key, areas = TYPES[job["type"]]
        p = PERSONAS[persona_key]
        months = job["horizon"] * 12
        paid_in = START + MONTHLY * months
        spec = GoalSpec(start_amount=START, monthly_flow=MONTHLY, goal_amount=round(job["multiple"] * paid_in, -3),
                        horizon_years=job["horizon"])
        cfg = AgentConfig(**({"goal_min_history_days": job["min_history"]} if job.get("min_history") else {}))
        answers = p.answers()          # the persona's own answers (its C-level); the goal's start is START
        t0 = time.perf_counter()
        run = run_pipeline(answers, investor_id=f"z2-{job['type']}", interest_areas=areas, goal=spec,
                           goal_type=p.goal_type, horizon_years=job["horizon"], as_of=t,
                           price_source=CutoffCacheSource(t), agent_config=cfg,
                           min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need)
        row["seconds"] = round(time.perf_counter() - t0, 1)
        g = run.goal_analysis
        row["c_level"] = run.profile.c_level
        row["goal_amount"] = spec.goal_amount
        if g is None:
            row.update(status="no_simulation", detail=run.goal_unavailable)
            return row
        chosen = next(o for o in g.outcomes if o.menu_index == g.chosen_index)
        final = g.fan[-1]
        weights = {a: w for a, w in run.result.final.weights.items() if w > 1e-9}
        fees = {i.ticker: i.expense_ratio for i in run.agent_c.catalog if i.ticker in weights}
        real, why = realized_final(weights, fees, t, months, MONTHLY, START)
        row.update(status="ok" if real is not None else "no_outcome", detail=why or None,
                   approved=run.approved, history_start=g.history_start, history_days=g.history_days,
                   n_backfilled=len(g.backfilled), shortlist=len(g.outcomes), equity=round(chosen.equity_weight, 3),
                   weights={a: round(w, 4) for a, w in sorted(weights.items(), key=lambda kv: -kv[1])},
                   predicted=chosen.prob_goal, **{k: round(getattr(final, k), 2) for k in PCTS},
                   realized=None if real is None else round(real, 2),
                   reached=None if real is None else int(real >= spec.goal_amount),
                   anchor_tp=job.get("anchor_tp"), short_rate=_ANCHOR["short_rate"])
        if real is not None:
            row["band"] = next((k for k in PCTS if real < getattr(final, k)), "above_p90")
    except Exception as exc:                                     # record, never stop the sweep
        row.update(status="error", detail=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-1500:])
    return row


# --------------------------------------------------------------------------------------------------------------
# parent side
# --------------------------------------------------------------------------------------------------------------
def jobs_for(run_name: str, anchor_tp: float | None = None) -> list[dict]:
    if anchor_tp is not None:                     # the main run's jobs with anchored bond/cash means
        return [{**j, "run": f"anchor_tp{anchor_tp:g}", "anchor_tp": anchor_tp} for j in jobs_for("main")]
    jobs = []
    if run_name == "main":
        dates = quarter_ends(date(2012, 3, 31), date(2021, 12, 31))
        horizons, min_history = (5, 10), None
    else:
        dates = quarter_ends(date(2008, 3, 31), date(2012, 3, 31))
        horizons, min_history = (5, 10), 252
    for t in dates:
        for h in horizons:
            if t + timedelta(days=round(h * 365.25) + 45) > LAST_CLOSE:      # whole horizon must be in the data
                continue
            for typ in TYPES:
                for m in GOAL_MULTIPLES[h]:
                    jobs.append({"run": run_name, "t": t.isoformat(), "type": typ, "horizon": h, "multiple": m,
                                 "min_history": min_history})
    return jobs


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarize(rows: list[dict], run_name: str, horizon: int | None) -> dict:
    ok = [r for r in rows if r["run"] == run_name and r.get("status") == "ok" and (horizon is None or r["horizon"] == horizon)]
    n = len(ok)
    if not n:
        return {"n": 0}
    bins = []
    for lo in range(0, 100, 10):
        hi = lo + 10
        inside = [r for r in ok if lo / 100 <= r["predicted"] < hi / 100 or (hi == 100 and r["predicted"] == 1.0)]
        k = sum(r["reached"] for r in inside)
        ci = wilson(k, len(inside))
        bins.append({"bin": f"{lo}-{hi}%", "n": len(inside),
                     "mean_predicted": round(sum(r["predicted"] for r in inside) / len(inside), 3) if inside else None,
                     "realized": round(k / len(inside), 3) if inside else None,
                     "ci95": [round(ci[0], 3), round(ci[1], 3)] if inside else None})
    brier = sum((r["predicted"] - r["reached"]) ** 2 for r in ok) / n
    base = sum(r["reached"] for r in ok) / n
    brier_climate = sum((base - r["reached"]) ** 2 for r in ok) / n
    bands = {b: sum(r["band"] == b for r in ok) / n for b in (*PCTS, "above_p90")}
    starts = sorted({r["t"] for r in ok})
    return {
        "n": n, "start_dates": len(starts), "first": starts[0], "last": starts[-1],
        "mean_predicted": round(sum(r["predicted"] for r in ok) / n, 3), "realized_rate": round(base, 3),
        "brier": round(brier, 4), "brier_always_base_rate": round(brier_climate, 4),
        "brier_skill": round(1 - brier / brier_climate, 3) if brier_climate > 0 else None,
        "band_shares": {k: round(v, 3) for k, v in bands.items()},          # expected .10 .15 .25 .25 .15 .10
        "inside_p10_p90": round(1 - bands["p10"] - bands["above_p90"], 3),     # expected 0.80
        "below_p50": round(bands["p10"] + bands["p25"] + bands["p50"], 3),      # expected 0.50
        "reliability": bins,
        "by_type": {typ: {"n": len(x), "mean_predicted": round(sum(r["predicted"] for r in x) / len(x), 3),
                          "realized": round(sum(r["reached"] for r in x) / len(x), 3)}
                    for typ in TYPES if (x := [r for r in ok if r["type"] == typ])},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--only", choices=["main", "sensitivity"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--anchor-tp", type=float, nargs="*", help="run ONLY these anchored-mean experiments")
    args = ap.parse_args()
    if args.anchor_tp:
        jobs = [j for tp in args.anchor_tp for j in jobs_for("main", tp)]
    else:
        jobs = [j for name in ("main", "sensitivity") if args.only in (None, name) for j in jobs_for(name)]
    if args.limit:
        jobs = jobs[: args.limit]
    print(f"{len(jobs)} runs on {args.workers} workers", flush=True)
    rows, t0 = [], time.time()
    with ProcessPoolExecutor(args.workers, initializer=_init_worker) as pool:
        futures = [pool.submit(run_one, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            rows.append(fut.result())
            if i % 25 == 0 or i == len(jobs):
                bad = sum(r.get("status") not in ("ok",) for r in rows)
                print(f"{i}/{len(jobs)} done, {bad} not ok, {time.time() - t0:.0f}s", flush=True)
    rows.sort(key=lambda r: (r["run"], r["t"], r["type"], r["horizon"], r["multiple"]))
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = offline.ROOT / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    summary = {name: {f"{h}y" if h else "all": summarize(rows, name, h) for h in (None, 5, 10)}
               for name in sorted({r["run"] for r in rows})}
    (out / f"zc_z2_calibration_{stamp}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1),
                                                        encoding="utf-8")
    cols = ["run", "t", "type", "c_level", "horizon", "multiple", "goal_amount", "status", "approved", "history_start",
            "history_days", "n_backfilled", "shortlist", "equity", "predicted", *PCTS, "realized", "reached", "band",
            "anchor_tp", "short_rate", "seconds", "detail"]
    lines = [",".join(cols)] + [",".join("" if r.get(c) is None else str(r.get(c)).replace(",", ";") for c in cols)
                                for r in rows]
    (out / f"zc_z2_calibration_{stamp}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"written: eval/results/zc_z2_calibration_{stamp}.{{csv,json}}")


if __name__ == "__main__":
    main()
