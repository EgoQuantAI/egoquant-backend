"""Peak memory of goal requests as the horizon grows, against the server's 512 MB.

    python eval/memory/horizon_memory.py                       # every case below, each in a fresh process
    python eval/memory/horizon_memory.py --case MODE PERSONA YEARS    (one case; MODE single | whatif | pair)

Through the real HTTP layer (FastAPI TestClient: the /portfolio route, report, sealed contexts), offline on the frozen
price cache with the network blocked (eval/regime/offline.py). The regime sampler runs (EGOQUANT_REGIME_MC=1, the
production default). Peak = the process's peak working set (Windows; Linux RSS on Railway is of the same order, so
keep a margin). Modes:
    single  one goal request
    whatif  one goal request, then a What-If on its report that moves the goal date to WHATIF_YEARS (the longest
            scenario the What-If Agent allows); the peak reported is the What-If's
    pair    two identical goal requests at the same moment (two threads)
Writes eval/results/memory_horizon.jsonl and eval/results/memory_horizon.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"
PERSONAS = ("balanced_saver", "growth_investor", "crypto_trader")     # the largest shortlists (Z1)
YEARS = (10, 25, 30, 40, 60)
WHATIF_YEARS = 40.0                                                    # what_if.HORIZON_BOUNDS[1]
BUDGET_MB = 450                                                        # 512 MB minus headroom for Linux allocators


def _body(persona: str, years: float) -> dict:
    from egoquant.demo.goal_walkthrough import default_goal
    from egoquant.demo.personas import PERSONAS as P
    p, spec = P[persona], default_goal(persona, years=years)
    return {"answers": {"choices": p.choices, "multi_choices": {"K4": p.k4}, "amount_invested": p.amount_invested,
                        "amount_needed": p.amount_needed},
            "preferences": {"preferences_version": p.preference_answers().preferences_version, "choices": p.preferences},
            "trading_platforms": p.trading_platforms, "horizon_years": years, "liquidity_need": p.liquidity_need,
            "min_cash_buffer": p.min_cash_buffer, "goal_amount": spec.goal_amount, "monthly_flow": spec.monthly_flow}


def case(mode: str, persona: str, years: float) -> dict:
    os.environ.setdefault("EGOQUANT_REGIME_MC", "1")
    sys.path.insert(0, str(ROOT))
    from eval.regime import offline
    offline.install_routes()
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    client.get("/data/status")
    body = _body(persona, years)
    state = {"peak": 0.0, "run": True}

    def sample():
        while state["run"]:
            state["peak"] = max(state["peak"], offline.process_memory_mb()[0])
            time.sleep(0.005)

    def start_sampling():
        state["peak"], state["run"] = offline.process_memory_mb()[0], True
        threading.Thread(target=sample, daemon=True).start()

    base = offline.process_memory_mb()[0]
    out: dict = {"mode": mode, "persona": persona, "years": years, "baseline_mb": round(base, 1)}
    t0 = time.perf_counter()
    if mode == "pair":
        start_sampling()
        replies: list = [None, None]

        def post(i):
            try:
                replies[i] = TestClient(app).post("/portfolio", json=body).status_code     # one client per thread
            except Exception as err:                                                       # noqa: BLE001
                replies[i] = f"{type(err).__name__}: {err}"[:200]

        threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out["status"] = replies
    else:
        if mode == "single":
            start_sampling()
        r = client.post("/portfolio", json=body)
        out["status"] = r.status_code
        if mode == "whatif" and r.status_code == 200:
            from egoquant.agents import what_if as wi
            start_sampling()
            t0 = time.perf_counter()
            res = wi.run_what_if(r.json()["what_if_context"], {"horizon_years": WHATIF_YEARS}, timeout_s=300)
            out["what_if_status"] = res.status
    out["seconds"] = round(time.perf_counter() - t0, 2)
    state["run"] = False
    time.sleep(0.02)
    out["peak_mb"] = round(state["peak"], 1)
    return out


def main() -> int:
    if "--case" in sys.argv:
        i = sys.argv.index("--case")
        print("CASE " + json.dumps(case(sys.argv[i + 1], sys.argv[i + 2], float(sys.argv[i + 3]))), flush=True)
        return 0
    plan = [("single", p, y) for p in PERSONAS for y in YEARS]
    plan += [("whatif", "balanced_saver", y) for y in (10, 25, 30)]
    plan += [("pair", "balanced_saver", y) for y in (25, 30, 60)]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    old = []
    if only and (RESULTS / "memory_horizon.jsonl").exists():          # re-measure one mode, keep the others
        old = [json.loads(ln) for ln in (RESULTS / "memory_horizon.jsonl").read_text(encoding="utf-8").splitlines()]
        old = [r for r in old if r["mode"] != only]
        plan = [c for c in plan if c[0] == only]
    rows = old
    for mode, persona, years in plan:
        proc = subprocess.run([sys.executable, __file__, "--case", mode, persona, str(years)], capture_output=True,
                              text=True, cwd=str(ROOT))
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("CASE ")]
        row = json.loads(line[-1][5:]) if line else {"mode": mode, "persona": persona, "years": years,
                                                      "error": proc.stderr[-600:]}
        rows.append(row)
        print(row, flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "memory_horizon.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    L = ["# Peak memory of goal requests by horizon (512 MB server)", "",
         "Offline, real HTTP layer, regime sampler on, peak working set per case in a fresh process (Windows). "
         f"Budget used here: {BUDGET_MB} MB (512 MB minus headroom). `eval/memory/horizon_memory.py`.", "",
         "| Mode | Investor | Years | Status | Time | Idle | Peak | Within budget |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            L.append(f"| {r['mode']} | {r['persona']} | {r['years']:g} | error | | | | |")
            continue
        status = r.get("what_if_status") or r["status"]
        L.append(f"| {r['mode']} | {r['persona']} | {r['years']:g} | {status} | {r['seconds']} s | "
                 f"{r['baseline_mb']} MB | {r['peak_mb']} MB | {'yes' if r['peak_mb'] <= BUDGET_MB else '**no**'} |")
    (RESULTS / "memory_horizon.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
