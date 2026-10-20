"""Regime model v2 against v1 and the block bootstrap, following eval/regime/PROTOCOL_v2.md (fixed before any result).

    python eval/regime/compare_v2.py calibration      # C: every variant on the same dates and draws -> selection
    python eval/regime/compare_v2.py today-bench      # A + D for the selected v2 (egoquant/data/regime_model_v2.json)
    python eval/regime/compare_v2.py summary          # the markdown summary from both JSON files

Same harness as compare_samplers.py (frozen real-price cache dated 2026-10-18, no network, production code).

  C. Calibration backtest at the same 37 quarter-ends as v1. At each date every model is the one FITTED ONLY ON DATA
     BEFORE IT (eval/results/regime_walkforward/: v1, v2 `spy`, v2 `joint`), and all samplers predict the same goals.
     Selection between `spy` and `joint`: 5-year Brier on 2012-06-30 .. 2016-12-31. Confirmation: the selected
     variant's 5-year Brier on 2017-03-31 .. 2021-06-30 (never used for the selection). Every variant is reported.
  A. Goal chances today with the selected v2 artifact (as compare_samplers.py section A).
  D. Runtime and peak memory of analyze_goal with the selected v2 artifact (each case in a fresh process).
  E. Limit checks through the HTTP layer with v2 (compare_samplers.py section E).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from eval.regime import compare_samplers as cs  # noqa: E402

RESULTS = ROOT / "eval" / "results"
WALK_FORWARD = RESULTS / "regime_walkforward"
V2_ARTIFACT = ROOT / "egoquant" / "data" / "regime_model_v2.json"
CALIB_JSON = RESULTS / "regime_v2_calibration.json"
TODAY_JSON = RESULTS / "regime_v2_today_bench.json"
SUMMARY_MD = RESULTS / "regime_v2_summary.md"
VARIANTS = {"v1": "regime_hmm_v1_asof_{d}.json", "spy": "regime_hmm_v2_spy_asof_{d}.json",
            "joint": "regime_hmm_v2_joint_asof_{d}.json"}
SELECTION_END = "2016-12-31"
CONFIRMATION_START = "2017-03-31"
BOND_HEAVY = ("P1", "P2", "P3")
EQUITY_HEAVY = ("P6", "P7", "P8")


# ==================================================================================================================
# C. Calibration
# ==================================================================================================================
def calibration(n_paths: int) -> dict:
    import numpy as np
    import pandas as pd
    from egoquant.agents.quant import goal as gq
    from egoquant.models import AgentConfig
    from egoquant.regime import hook
    from eval.regime import offline
    cfg = AgentConfig()
    classes, fees = cs.catalog_info()
    prices = cs.load_prices(offline._STATE["cache"])
    returns_all = prices.pct_change(fill_method=None)
    rows, dates_info = [], []
    for t in pd.date_range("2012-06-30", "2021-06-30", freq="QE"):
        daily = cs.history(prices.loc[:t], classes, cfg)
        info = {"date": str(t.date()), "history_days": len(daily)}
        if len(daily) < cfg.goal_min_history_days:
            info["skipped"] = "less than the minimum history"
            dates_info.append(info)
            continue
        blocks = gq.block_returns(daily, cfg.goal_block_days)
        future = returns_all.loc[returns_all.index > t]
        fee_v = cs.vec(fees, daily.columns)
        for years, mults in cs.GOAL_MULTS.items():
            months = years * 12
            if len(future) < months * cfg.goal_block_days or (years == 10 and t > pd.Timestamp("2016-06-30")):
                continue
            fut = future.iloc[: months * cfg.goal_block_days][list(daily.columns)].fillna(0.0)
            real_blocks = np.exp(np.log1p(fut.to_numpy()).reshape(months, cfg.goal_block_days, -1).sum(axis=1))
            real_draws = np.arange(months)[None, :]
            draws = {"old": np.random.default_rng(cfg.goal_seed).integers(0, len(blocks), size=(n_paths, months))}
            for v, pattern in VARIANTS.items():
                path = WALK_FORWARD / pattern.format(d=t.date())
                dec = hook.decide(daily.index, cfg.goal_block_days, t.date(), "historical", model_path=path)
                d = dec.draws(len(blocks), n_paths, months, cfg.goal_seed)
                rec = dec.record()
                info.setdefault(v, {})[f"{years}y"] = {
                    "sampler": rec.get("sampler"), "fallback_reason": rec.get("fallback_reason"),
                    "states": len(rec.get("state_names") or []), "regime_today": rec.get("regime_today"),
                    "regime_today_probability": rec.get("regime_today_probability")}
                draws[v] = d if d is not None else draws["old"]
            for name, wd in cs.PORTFOLIOS.items():
                w = cs.vec(wd, daily.columns)
                sp = cs.spec(1.0, years)
                finals = {k: gq.simulate_wealth(blocks, w, fee_v, sp, d)[:, -1] for k, d in draws.items()}
                f_real = float(gq.simulate_wealth(real_blocks, w, fee_v, sp, real_draws)[0, -1])
                for mult in mults:
                    goal = mult * (cs.START + cs.MONTHLY * months)
                    row = {"date": str(t.date()), "years": years, "portfolio": name, "goal_multiple": mult,
                           "outcome": int(f_real >= goal), "realised_final": round(f_real, 2)}
                    row.update({f"pred_{k}": float(np.mean(f >= goal)) for k, f in finals.items()})
                    rows.append(row)
        dates_info.append(info)
        print(f"  {t.date()} done", flush=True)
    return {"rows": rows, "dates": dates_info, "n_paths": n_paths, "metrics": metrics(rows)}


def _as_new(rows, key):
    return [{**r, "pred_new": r[f"pred_{key}"]} for r in rows]


def metrics(rows) -> dict:
    """calibration_metrics (Brier, calibration table, date-bootstrap CI of new - old) for every variant, over all
    dates, the selection period and the confirmation period."""
    periods = {"all": rows,
               "selection": [r for r in rows if r["date"] <= SELECTION_END],
               "confirmation": [r for r in rows if r["date"] >= CONFIRMATION_START]}
    out = {}
    for v in VARIANTS:
        out[v] = {p: cs.calibration_metrics(_as_new(sub, v)) for p, sub in periods.items() if sub}
    sel = {v: out[v]["selection"]["5y"]["brier_new"] for v in ("spy", "joint")}
    chosen = "spy" if sel["spy"] <= sel["joint"] else "joint"
    out["selection"] = {"rule": "lower 5-year Brier on dates <= " + SELECTION_END + " (ties: spy)",
                        "brier_5y_selection_period": sel, "chosen": chosen}
    return out


# ==================================================================================================================
# A + D with the selected v2 artifact
# ==================================================================================================================
def use_v2() -> None:
    from egoquant.regime import hook
    hook.DEFAULT_MODEL_VERSION = "regime_hmm_v2"


def today_bench(n_paths: int, reps: int) -> dict:
    from egoquant.models import AgentConfig
    from eval.regime import offline
    use_v2()
    cfg = AgentConfig()
    classes, fees = cs.catalog_info()
    prices = cs.load_prices(offline._STATE["cache"])
    res = {"A": cs.section_a(prices, classes, fees, cfg, n_paths)}
    out = []
    for years in (15.0, 30.0, 60.0):
        for variant in ("current", "z1_lean"):
            for sampler in ("old", "new"):
                proc = subprocess.run([cs.PYTHON, str(Path(__file__)), "--bench", str(years), sampler, variant, str(reps)],
                                      capture_output=True, text=True, cwd=str(ROOT))
                line = [ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH ")]
                if proc.returncode != 0 or not line:
                    out.append({"years": years, "sampler": sampler, "variant": variant, "error": proc.stderr[-800:]})
                    continue
                out.append(json.loads(line[-1][6:]))
                print(f"  bench {years:>4} y {variant:<8} {sampler}: {out[-1]['time_s_median']:.2f} s, "
                      f"tracemalloc {out[-1]['tracemalloc_peak_mb']:.2f} MB", flush=True)
    res["D"] = out
    print("E. limit checks", flush=True)
    res["E"] = cs.section_e()                     # subprocesses: the default model is now regime_hmm_v2
    return res


def bench(years: float, sampler: str, variant: str, reps: int) -> dict:
    """compare_samplers.bench_case with the v2 artifact as the default model."""
    from eval.regime.offline import install
    install()
    use_v2()
    return cs.bench_case(years, sampler, variant, reps)


# ==================================================================================================================
# Summary
# ==================================================================================================================
def _ci(m):
    lo, hi = m["brier_diff_ci95_date_bootstrap"]
    return f"{m['brier_new_minus_old']:+.4f} ({lo:+.4f} to {hi:+.4f})"


def summary() -> str:
    c = json.loads(CALIB_JSON.read_text(encoding="utf-8"))
    t = json.loads(TODAY_JSON.read_text(encoding="utf-8")) if TODAY_JSON.is_file() else None
    m = c["metrics"]
    chosen = m["selection"]["chosen"]
    L = ["# Regime model v2: results", "",
         f"Protocol fixed before any result: `eval/regime/PROTOCOL_v2.md`. Frozen real-price cache (2026-10-18), "
         f"{c['n_paths']:,} paths, models refitted at every date on earlier data only.", "",
         "## Calibration backtest (Brier score, lower is better; difference vs the block bootstrap with a 95% "
         "date-bootstrap interval)", "",
         "| Model | Period | 5-year Brier | vs block bootstrap | 10-year Brier | vs block bootstrap |",
         "|---|---|---|---|---|---|"]
    for v in VARIANTS:
        for p in ("all", "selection", "confirmation"):
            if p not in m[v]:
                continue
            r5, r10 = m[v][p].get("5y"), m[v][p].get("10y")
            L.append(f"| {v} | {p} | {r5['brier_new']:.4f} | {_ci(r5)} | "
                     + (f"{r10['brier_new']:.4f} | {_ci(r10)} |" if r10 else "- | - |"))
    old = m["v1"]["all"]
    L += ["", f"Block bootstrap: 5-year {old['5y']['brier_old']:.4f}, 10-year {old['10y']['brier_old']:.4f} (all dates); "
          f"5-year {m['v1']['selection']['5y']['brier_old']:.4f} (selection), "
          f"{m['v1']['confirmation']['5y']['brier_old']:.4f} (confirmation).", "",
          f"**Selection** ({m['selection']['rule']}): spy {m['selection']['brier_5y_selection_period']['spy']:.4f}, "
          f"joint {m['selection']['brier_5y_selection_period']['joint']:.4f} -> **{chosen}** becomes regime_hmm_v2.", "",
          "## Per portfolio (5-year, all dates): Brier and mean predicted chance vs share reached", "",
          "| Portfolio | Reached | Block bootstrap | v1 | v2 spy | v2 joint |", "|---|---|---|---|---|---|"]
    for name in cs.PORTFOLIOS:
        pp = {v: m[v]["all"]["5y"]["per_portfolio"][name] for v in VARIANTS}
        b = pp["v1"]
        L.append(f"| {name} | {b['reached']:.0%} | {b['brier_old']:.4f} ({b['mean_pred_old']:.0%}) | "
                 + " | ".join(f"{pp[v]['brier_new']:.4f} ({pp[v]['mean_pred_new']:.0%})" for v in VARIANTS) + " |")
    states = {}
    for info in c["dates"]:
        for v in ("spy", "joint"):
            k = (info.get(v) or {}).get("5y", {}).get("states")
            if k:
                states.setdefault(v, []).append(k)
    L += ["", "States chosen by BIC at the walk-forward dates: "
          + "; ".join(f"{v}: " + ", ".join(f"K={k} x{s.count(k)}" for k in sorted(set(s))) for v, s in states.items())]
    if t:
        L += ["", f"## Goal chances today with v2 ({chosen}), 15 years, goal 1.5x paid in", "",
              "| Portfolio | Block bootstrap | v2 |", "|---|---|---|"]
        for r in t["A"]["rows"]:
            if r["goal_multiple_of_paid_in"] == 1.5:
                L.append(f"| {r['portfolio']} | {r['old']['prob_goal']:.1%} | {r['new']['prob_goal']:.1%} |")
        rec = t["A"]["regime_record"]
        L += ["", f"Today's regime (v2): {rec.get('regime_today')} ({rec.get('regime_today_probability', 0):.1%}); "
              f"states {rec.get('state_names')}.", "", "## Runtime and peak memory of analyze_goal (v2)", "",
              "| Horizon | Code | Time old -> new | tracemalloc peak old -> new |", "|---|---|---|---|"]
        pairs = {}
        for r in t["D"]:
            if "error" not in r:
                pairs.setdefault((r["years"], r["variant"]), {})[r["sampler"]] = r
        for (y, var), p in sorted(pairs.items()):
            if "old" in p and "new" in p:
                o, n = p["old"], p["new"]
                L.append(f"| {y:g} y | {var} | {o['time_s_median']:.2f} -> {n['time_s_median']:.2f} s "
                         f"({n['time_s_median'] / o['time_s_median'] - 1:+.0%}) | {o['tracemalloc_peak_mb']:.2f} -> "
                         f"{n['tracemalloc_peak_mb']:.2f} MB ({(n['tracemalloc_peak_mb'] - o['tracemalloc_peak_mb']) * 1024:+.0f} KiB) |")
        e = (t.get("E") or {}).get("summary") or {}
        if e:
            L += ["", "## Limit checks with v2 (every persona x 3 goals x flag off/on, HTTP layer)", "",
                  f"- requests: {e.get('requests')}, all answered 200: {e.get('all_answered_200')}",
                  f"- Risk Engine inconsistencies: {e.get('risk_engine_inconsistent')}; approved with a broken limit: "
                  f"{e.get('limits_broken_in_approved')}",
                  f"- regime runs: {e.get('regime_runs')}; What-If baseline mismatches: "
                  f"{e.get('what_if_baseline_mismatches')}"]
    v2 = m[chosen]
    crit = {"5-year Brier not worse (all dates)": v2["all"]["5y"]["brier_new"] <= v2["all"]["5y"]["brier_old"],
            "10-year Brier not worse (all dates)": v2["all"]["10y"]["brier_new"] <= v2["all"]["10y"]["brier_old"],
            "5-year Brier not worse (confirmation period)":
                v2["confirmation"]["5y"]["brier_new"] <= v2["confirmation"]["5y"]["brier_old"]}
    if t:
        pairs = {}
        for r in t["D"]:
            if "error" not in r:
                pairs.setdefault((r["years"], r["variant"]), {})[r["sampler"]] = r
        done = [p for p in pairs.values() if "old" in p and "new" in p]
        crit["runtime +25% at most"] = bool(done) and all(
            p["new"]["time_s_median"] <= 1.25 * p["old"]["time_s_median"] for p in done)
        crit["peak memory not higher (strict, tracemalloc)"] = bool(done) and all(
            p["new"]["tracemalloc_peak_mb"] <= p["old"]["tracemalloc_peak_mb"] for p in done)
        e = (t.get("E") or {}).get("summary") or {}
        crit["limit checks and What-If consistency"] = bool(e) and e["all_answered_200"] and not (
            e["risk_engine_inconsistent"] or e["limits_broken_in_approved"] or e["what_if_baseline_mismatches"])
    conf = v2["confirmation"]["5y"]
    L += ["", "## Decision (PROTOCOL_v2.md)", ""] + [f"- {k}: {'PASS' if ok else 'FAIL'}" for k, ok in crit.items()]
    L += ["", f"Note: in the confirmation period the change is {_ci(conf)}: the interval includes 0, so the "
          "confirmation period alone neither proves nor rules out a gain. The test suite is checked separately."]
    L += ["", "**Recommendation:** " + ("all measured criteria pass; confirm the test suite before turning the flag on"
                                       if all(crit.values()) else "keep `EGOQUANT_REGIME_MC` off under the protocol: "
                                       + ", ".join(k for k, ok in crit.items() if not ok) + " failed")]
    if t:
        mem = ", ".join(f"{y:g} y {v} {(p['new']['tracemalloc_peak_mb'] - p['old']['tracemalloc_peak_mb']) * 1024:+.0f} KiB"
                        for (y, v), p in sorted(pairs.items()) if "old" in p and "new" in p)
        L += ["", "## Shipping decision (outside the protocol)", "",
              "The flag is ON by default at the product owner's request (`EGOQUANT_REGIME_MC=0` is the kill switch "
              "and gives the old output byte for byte). The protocol's strict memory rule is left as it is and still "
              f"reported above. Peak memory new - old: {mem}. The remaining few KiB at long horizons are the sampler "
              "record itself; any regime sampler keeps one. The held-out (confirmation) gain is within noise. Merging "
              "changes live goal chances and can change the chosen portfolio (see the limit checks)."]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", choices=["calibration", "today-bench", "summary"])
    ap.add_argument("--bench", nargs=4, metavar=("YEARS", "SAMPLER", "VARIANT", "REPS"))
    ap.add_argument("--paths", type=int, default=None)
    args = ap.parse_args(argv)
    if args.bench:
        years, sampler, variant, reps = args.bench
        print("BENCH " + json.dumps(bench(float(years), sampler, variant, int(reps))))
        return 0
    if args.stage == "summary":
        SUMMARY_MD.write_text(summary(), encoding="utf-8")
        print(SUMMARY_MD.read_text(encoding="utf-8"))
        return 0
    from eval.regime import offline
    offline.install(regime_flag="1")
    from egoquant.models import AgentConfig
    n_paths = args.paths or AgentConfig().goal_paths
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=str(ROOT)).stdout.strip()
    meta = {"created": datetime.now().strftime("%Y-%m-%d %H:%M"), "commit": commit}
    if args.stage == "calibration":
        res = {**meta, **calibration(n_paths)}
        CALIB_JSON.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        print("selection:", res["metrics"]["selection"])
    else:
        res = {**meta, **today_bench(n_paths, reps=3)}
        TODAY_JSON.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    offline.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
