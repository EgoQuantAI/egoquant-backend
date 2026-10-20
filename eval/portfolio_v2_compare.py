"""Portfolio method v1 against v2 for the six demo personas, and the acceptance checks of docs/PORTFOLIO_V2.md section 5.

    python eval/portfolio_v2_compare.py                       # 6 personas x (no goal, 15-year goal) x (v1, v2), 24 runs
    python eval/portfolio_v2_compare.py --methods v1          # v1 only: works today, v2 checks print N/A
    python eval/portfolio_v2_compare.py --personas balanced_saver,growth_investor --goals none --jobs 2
    python eval/portfolio_v2_compare.py --goal-years 30       # the contract's memory case
    python eval/portfolio_v2_compare.py --json out.json --md table.md

Every run is its OWN subprocess (so time and peak memory are per run, and one run cannot influence another): the flag
EGOQUANT_PORTFOLIO_METHOD is unset for v1 and "v2" for v2. Offline and free: no key is read, every non-loopback
connection is refused, so Agent C uses the committed Yahoo snapshot of REAL prices (as of 2026-10-16) exactly as the
deployed app does when its feeds are down; no OpenAI call, no Tiingo call, no briefing.

The goal of each persona: its own start amount, no monthly flow, and the amount that a steady yearly growth of the
persona's risk class would reach (C1 2.5%, C2 3.5%, C3 4.5%, C4 5.5%, C5 6.5%, PROVISIONAL), over --goal-years (15).

What the columns are (all read off the pipeline result, nothing is estimated here):
  holdings   products with a weight of at least 0.1%           effN      1 / sum(w^2)         largest  biggest single weight
  stocks     equity share of the portfolio                     chance    goal chance of the chosen portfolio (goal runs)
  dd90       worst-10% drawdown: the 90th percentile over simulated paths of each path's largest peak-to-trough fall
             (goal runs only; without a goal there is no simulation)       hist DD   in-sample drawdown of the window
  cpu s      CPU seconds of the run (the contract's "work"; wall seconds are shown too)     MB   peak memory of the run

The acceptance checks print PASS, FAIL, N/A (cannot be measured, e.g. v2 not active yet) or MANUAL. Exit code 1 when a
check FAILs. If any item fails, the flag stays v1 (contract section 5).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 16)                       # the last close in the committed Yahoo snapshot
PERSONA_ORDER = ["no_loss_saver", "cautious_retiree", "balanced_saver", "growth_investor", "crypto_trader",
                 "eager_beginner"]
GOAL_GROWTH = {"C1": 0.025, "C2": 0.035, "C3": 0.045, "C4": 0.055, "C5": 0.065}      # PROVISIONAL, a goal per class
HOLDING_MIN = 0.001
SECRET_ENV = ("TIINGO_API_KEY", "TIINGO_WARM_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA", "EGOQUANT_DATA_FALLBACK",
              "EGOQUANT_PRICE_SNAPSHOT", "EGOQUANT_PORTFOLIO_METHOD")
# Contract section 5 numbers (PROVISIONAL, as decided by Savas on 19 Oct)
MIN_EFFECTIVE_N, MAX_LARGEST, TIME_LIMIT_S, MEMORY_LIMIT_MB = 6.0, 0.30, 45.0, 450.0


# ==========================================================================
# One run (this is what a subprocess does)
# ==========================================================================
def peak_memory_mb() -> float:
    """Peak resident memory of this process so far, in MB (Windows: peak working set; Linux/macOS: ru_maxrss)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1e6
        return float("nan")
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def _go_offline() -> None:
    """No keys, no background workers, no non-loopback network (asyncio's own loopback socketpair must keep working)."""
    import socket
    for name in SECRET_ENV:
        if name != "EGOQUANT_PORTFOLIO_METHOD":           # the parent decides the method
            os.environ.pop(name, None)
    os.environ.update({"EGOQUANT_WARM": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_BRIEFING_LLM": "0",
                       "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off", "EGOQUANT_CONTEXT_SECRET": "portfolio-v2-compare",
                       "EGOQUANT_CACHE_DIR": tempfile.mkdtemp(prefix="egoquant_v2_cache_")})
    loopback = ("127.0.0.1", "::1", "localhost", "0.0.0.0")
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def host(address) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    def guarded_connect(self, address):
        if host(address) not in loopback:
            raise OSError("network blocked for the offline comparison")
        return real_connect(self, address)

    def guarded_getaddrinfo(name, *args, **kwargs):
        if name is not None and str(name) not in loopback:
            raise OSError("network blocked for the offline comparison")
        return real_getaddrinfo(name, *args, **kwargs)

    socket.socket.connect = guarded_connect               # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo              # type: ignore[assignment]


def v2_diagnostics(diagnostics: dict) -> dict | None:
    """The v2 information of a proposal, without the prefix: the flat `v2_*` keys of docs/PORTFOLIO_V2.md section 4
    (v2_style, v2_effective_n, v2_largest, v2_n_holdings, v2_floor_met, v2_worst10_drawdown, v2_risk_contribution as a
    JSON string, ...). A nested `method_v2` object (the first draft of the contract), as a dict or a JSON string, is read
    too. None when the portfolio carries neither: it was not built by method v2."""
    flat = {key[3:]: value for key, value in diagnostics.items() if key.startswith("v2_")}
    if flat:
        for key, value in list(flat.items()):
            if key == "risk_contribution" and isinstance(value, str):
                try:
                    flat[key] = json.loads(value)
                except ValueError:
                    pass
        return flat
    nested = diagnostics.get("method_v2")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = {"raw": nested}
    return nested if isinstance(nested, dict) and nested else None


def run_one(persona_key: str, goal_years: float | None) -> dict:
    """The full pipeline for one persona, offline; returns the numbers of this module's table as a plain dict."""
    import time

    _go_offline()
    sys.path.insert(0, str(ROOT))
    from egoquant.agents.quant.goal import GoalSpec
    from egoquant.demo.personas import PERSONAS, behavioral_agent_for
    from egoquant.pipeline import run_pipeline

    p = PERSONAS[persona_key]
    goal = None
    if goal_years:
        growth = GOAL_GROWTH[p.expected_class]
        goal = GoalSpec(start_amount=p.amount_invested, monthly_flow=0.0,
                        goal_amount=round(p.amount_invested * (1 + growth) ** goal_years, -2),
                        horizon_years=goal_years)
    wall, cpu = time.perf_counter(), time.process_time()
    run = run_pipeline(
        p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
        goal_type=p.goal_type, horizon_years=goal_years or p.horizon_years, as_of=AS_OF,
        trading_platforms=p.trading_platforms, behavioral_agent=behavioral_agent_for(p), price_source=None,
        min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need, current_holdings=p.current_holdings,
        goal=goal, directory=None)
    cpu, wall = time.process_time() - cpu, time.perf_counter() - wall

    final = run.result.final
    weights = {a: w for a, w in sorted(final.weights.items()) if w > 1e-9}
    chance = dd90 = None
    if run.goal_analysis is not None:
        chosen = [o for o in run.goal_analysis.outcomes if o.menu_index == run.goal_analysis.chosen_index]
        if chosen:
            chance, dd90 = chosen[0].prob_goal, chosen[0].drawdown_p90
    diag = v2_diagnostics(final.diagnostics or {})
    if dd90 is None and diag:
        dd90 = diag.get("worst10_drawdown")                   # contract section 4, when the goal analysis has none
    return {
        "persona": persona_key, "c_level": run.profile.c_level, "goal": f"{goal_years:g}y" if goal_years else "none",
        "ok": True, "error": None, "approved": bool(run.approved), "status": final.status,
        "n_holdings": sum(w >= HOLDING_MIN for w in weights.values()),
        "eff_n": final.risk.effective_n_holdings if final.risk else None,
        "largest": max(weights.values()) if weights else None, "stock_share": final.equity_weight,
        "chance": chance, "dd90": dd90, "hist_dd": final.risk.hist_max_drawdown if final.risk else None,
        "n_universe": len(run.selection.instruments), "weights": weights, "v2": diag,
        "seed": diag.get("seed") if isinstance(diag, dict) else None, "v2_style": diag.get("style") if diag else None, "goal_unavailable": run.goal_unavailable,
        "data_source": run.report.data_source, "cpu_s": cpu, "wall_s": wall, "peak_mb": peak_memory_mb(),
    }


# ==========================================================================
# Running all cases in subprocesses
# ==========================================================================
@dataclass(frozen=True)
class Case:
    persona: str
    goal_years: float | None
    method: str                      # "v1" | "v2"
    repeat: bool = False             # the second run of the determinism check

    @property
    def goal(self) -> str:
        return f"{self.goal_years:g}y" if self.goal_years else "none"


def run_case(case: Case, timeout: float) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if case.method == "v2":
        env["EGOQUANT_PORTFOLIO_METHOD"] = "v2"
    with tempfile.TemporaryDirectory(prefix="v2_compare_") as tmp:
        out = Path(tmp) / "row.json"
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--persona", case.persona,
               "--goal-years", str(case.goal_years or 0), "--out", str(out)]
        failed = {"persona": case.persona, "goal": case.goal, "ok": False}
        try:
            done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            return {**failed, "error": f"timed out after {timeout:.0f} s"}
        if done.returncode != 0 or not out.exists():
            lines = [x for x in (done.stderr or done.stdout).strip().splitlines() if x.strip()]
            return {**failed, "error": (lines[-1] if lines else f"exit code {done.returncode}")[:300]}
        return json.loads(out.read_text(encoding="utf-8"))


def run_all(personas: list[str], goals: list[float | None], methods: list[str], jobs: int, timeout: float,
            determinism: bool = True) -> tuple[list[dict], list[dict]]:
    cases = [Case(p, g, m) for p in personas for g in goals for m in methods]
    repeats = []
    if determinism and cases:
        # the same input once more, for the cheapest requested case (no goal when asked for: no simulation)
        persona = "balanced_saver" if "balanced_saver" in personas else personas[0]
        goal = None if None in goals else goals[0]
        repeats = [Case(persona, goal, m, repeat=True) for m in methods]
    work = cases + repeats
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda c: run_case(c, timeout), work))
    rows = []
    for case, row in zip(cases, results[:len(cases)]):
        rows.append({**row, "method": case.method})
    again = [{**row, "method": case.method} for case, row in zip(repeats, results[len(cases):])]
    return rows, again


# ==========================================================================
# Table and acceptance checks (pure functions of the rows, tested without running anything)
# ==========================================================================
def _pct(x, digits=0) -> str:
    return "-" if x is None else f"{100 * x:.{digits}f}%"


def _num(x, fmt) -> str:
    return "-" if x is None else format(x, fmt)


def _same_weights(a: dict, b: dict) -> bool:
    return json.dumps(a["weights"], sort_keys=True) == json.dumps(b["weights"], sort_keys=True)


def _v1_rows(rows: list[dict]) -> dict:
    return {(r["persona"], r["goal"]): r for r in rows if r.get("ok") and r["method"] == "v1"}


def _inactive(r: dict, v1: dict) -> bool:
    """A v2 run in which the flag did nothing: no v2_* diagnostics and the very same portfolio as v1."""
    if not (r.get("ok") and r["method"] == "v2") or r.get("v2"):
        return False
    first = v1.get((r["persona"], r["goal"]))
    return first is not None and _same_weights(first, r)


def format_table(rows: list[dict]) -> str:
    v1 = _v1_rows(rows)
    head = ("persona", "cls", "goal", "method", "holdings", "effN", "largest", "stocks", "chance", "dd90", "hist DD",
            "universe", "cpu s", "wall s", "MB")
    lines = [head]
    for r in rows:
        if not r.get("ok"):
            lines.append((r["persona"], r.get("c_level", "?"), r["goal"], r["method"], "ERROR: " + str(r.get("error"))))
        elif _inactive(r, v1):
            lines.append((r["persona"], r["c_level"], r["goal"], r["method"]) + ("n/a",) * (len(head) - 4))
        else:
            label = r["method"] + ("?" if r["method"] == "v2" and not r.get("v2") else "")
            lines.append((r["persona"], r["c_level"], r["goal"], label, str(r["n_holdings"]),
                          _num(r["eff_n"], ".1f"), _pct(r["largest"]), _pct(r["stock_share"]), _pct(r["chance"]),
                          _pct(r["dd90"], 1), _pct(r["hist_dd"], 1), str(r["n_universe"]), _num(r["cpu_s"], ".1f"),
                          _num(r["wall_s"], ".1f"), _num(r["peak_mb"], ".0f")))
    widths = [max(len(line[i]) for line in lines if len(line) > i) for i in range(len(head))]
    out = []
    for n, line in enumerate(lines):
        out.append("  ".join(cell.ljust(widths[i]) if i < 4 else cell.rjust(widths[i]) for i, cell in enumerate(line)))
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    if any(_inactive(r, v1) for r in rows):
        out.append("n/a: v2 was requested, but the portfolio is the same as v1 and carries no v2_* diagnostics, so "
                   "the flag had no effect (v2 is not built into this branch yet). Nothing is compared.")
    if any(r.get("ok") and r["method"] == "v2" and not r.get("v2") and not _inactive(r, v1) for r in rows):
        out.append("v2?: the portfolio differs from v1 but carries no v2_* diagnostics (the report cannot show the method yet).")
    return "\n".join(out)


def format_holdings(rows: list[dict], top: int = 8) -> str:
    out = []
    for r in rows:
        if not r.get("ok"):
            continue
        best = sorted(r["weights"].items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        more = len(r["weights"]) - len(best)
        out.append(f"{r['persona']:<17} {r['goal']:<5} {r['method']}  " + ", ".join(f"{a} {100 * w:.0f}%" for a, w in best)
                   + (f", +{more} more" if more > 0 else ""))
    return "\n".join(out)


@dataclass(frozen=True)
class Check:
    name: str
    status: str                      # PASS | FAIL | N/A | MANUAL
    detail: str = ""


def tolerance(best_chance: float) -> float:
    """Contract 2.3: tau = max(1 point, min(3 points, 10% of the best chance))."""
    return max(0.01, min(0.03, 0.10 * best_chance))


def _breaches_v2_limits(r: dict) -> bool:
    """Whether a portfolio itself is outside what v2 allows for C3 to C5 (fund cap 30%, effective N 6)."""
    return r["c_level"] in ("C3", "C4", "C5") and (
        (r["largest"] or 0) > MAX_LARGEST + 1e-9 or (r["eff_n"] or 0) < MIN_EFFECTIVE_N - 1e-9)


def _v1_note(a: dict) -> str:
    """Appended to a chance or drawdown FAIL when the v1 portfolio it is held against is itself outside the v2 limits, so
    the comparison is not like for like."""
    if _breaches_v2_limits(a):
        return f" [v1 itself breaches the v2 limits: largest {_pct(a['largest'])}, effN {_num(a['eff_n'], '.1f')}]"
    return ""


def acceptance(rows: list[dict], repeats: list[dict] | None = None, drawdown_tie: float = 0.0) -> list[Check]:
    """drawdown_tie: how much higher than v1 a worst-10% drawdown may be (contract: 0; the engine treats drawdowns within
    one point as a tie and lets diversification decide, so it can end up to that much higher by design)."""
    ok = [r for r in rows if r.get("ok")]
    v1 = {(r["persona"], r["goal"]): r for r in ok if r["method"] == "v1"}
    v2 = {(r["persona"], r["goal"]): r for r in ok if r["method"] == "v2"}
    asked_v2 = any(r["method"] == "v2" for r in rows)
    errors = [f"{r['persona']}/{r['goal']}/{r['method']}: {r.get('error')}" for r in rows if not r.get("ok")]
    live = {k: r for k, r in v2.items() if r.get("v2") or not _inactive(r, v1)}      # the flag did something
    v2 = live                                                                         # the numeric checks look at these
    active = bool(live)
    na = "N/A"
    why = "v2 was not run" if not asked_v2 else "v2 is not active in these runs"
    checks: list[Check] = []

    def need_v2(name: str, evaluate) -> None:
        if not active:
            checks.append(Check(name, na, why))
        else:
            checks.append(evaluate())

    checks.append(Check("every run finished", "FAIL" if errors else "PASS", "; ".join(errors[:4])))
    if asked_v2:
        every_v2 = [r for r in ok if r["method"] == "v2"]
        missing = [f"{r['persona']}/{r['goal']}" for r in every_v2 if not r.get("v2")]
        checks.append(Check("v2 changes the portfolio (the flag has an effect)", "PASS" if active else "FAIL",
                            f"{len(live)} of {len(every_v2)} v2 portfolios differ from v1 or carry diagnostics"
                            if active else "no v2 portfolio differs from v1 and none carries v2_* diagnostics"))
        checks.append(Check("v2_* diagnostics in every v2 portfolio (the report shows the method)",
                            "FAIL" if missing or not every_v2 else "PASS", ("missing: " + ", ".join(missing[:6])) if missing else ""))
    else:
        checks.append(Check("v2 changes the portfolio (the flag has an effect)", na, why))
        checks.append(Check("v2_* diagnostics in every v2 portfolio (the report shows the method)", na, why))

    def approvals() -> Check:
        lost = [f"{k[0]}/{k[1]}" for k, a in v1.items() if a["approved"] and k in v2 and not v2[k]["approved"]]
        return Check("Risk Engine approves every v2 portfolio that v1 approved", "FAIL" if lost else "PASS",
                     ("rejected under v2: " + ", ".join(lost)) if lost else f"{len(v2)} portfolios")
    need_v2("Risk Engine approves every v2 portfolio that v1 approved", approvals)
    checks.append(Check("no hard limit ever violated (200 random investors)", "MANUAL", "unit test in the v2 suite, not in this script"))

    if repeats:
        same = {}
        for again in repeats:
            first = next((r for r in ok if r["method"] == again.get("method") and r["persona"] == again.get("persona")
                          and r["goal"] == again.get("goal")), None)
            if first is None or not again.get("ok"):
                same[again.get("method")] = None
            else:
                same[again.get("method")] = json.dumps(first["weights"], sort_keys=True) == json.dumps(
                    again["weights"], sort_keys=True)
        bad = [m for m, same_ in same.items() if same_ is not True]
        checks.append(Check("same input twice = byte-identical portfolio", "FAIL" if bad else "PASS",
                            ("differs or missing for " + ", ".join(map(str, bad))) if bad else
                            "checked for " + ", ".join(sorted(map(str, same)))))
    else:
        checks.append(Check("same input twice = byte-identical portfolio", na, "determinism rerun was off"))

    def seeds() -> Check:
        seen = [r["seed"] for r in v2.values()]
        if None in seen:
            return Check("different goal / amount / answers = different seed", "N/A",
                         f"{seen.count(None)} of {len(seen)} v2 runs report no seed (the engine does not export one; see "
                         "tests/agent_a/v2 for the seed rule)")
        dup = len(seen) - len(set(seen))
        return Check("different goal / amount / answers = different seed", "FAIL" if dup else "PASS",
                     f"{len(set(seen))} distinct seeds in {len(seen)} runs")
    need_v2("different goal / amount / answers = different seed", seeds)

    def diversification() -> Check:
        bad = []
        for k, r in v2.items():
            if r["c_level"] not in ("C3", "C4", "C5"):
                continue
            flagged = isinstance(r["v2"], dict) and (r["v2"].get("diversification_floor_met") is False
                                                     or r["v2"].get("floor_met") == 0)
            if not flagged and (r["eff_n"] is None or r["eff_n"] < MIN_EFFECTIVE_N - 1e-9
                                or r["largest"] is None or r["largest"] > MAX_LARGEST + 1e-9):
                bad.append(f"{k[0]}/{k[1]} (effN {_num(r['eff_n'], '.1f')}, largest {_pct(r['largest'])})")
        return Check("effective N >= 6 and largest holding <= 30% for C3 to C5 (or flagged)", "FAIL" if bad else "PASS",
                     "; ".join(bad))
    need_v2("effective N >= 6 and largest holding <= 30% for C3 to C5 (or flagged)", diversification)

    def chance() -> Check:
        rows_ = [(k, v1[k], r) for k, r in v2.items() if k in v1 and r["chance"] is not None and v1[k]["chance"] is not None]
        if not rows_:
            return Check("goal chance not lower than v1 by more than the tolerance", na, "no goal run with a chance")
        bad = [f"{k[0]} ({_pct(a['chance'])} -> {_pct(b['chance'])}, tolerance {_pct(tolerance(a['chance']), 1)}){_v1_note(a)}"
               for k, a, b in rows_ if b["chance"] < a["chance"] - tolerance(a["chance"]) - 1e-12]
        return Check("goal chance not lower than v1 by more than the tolerance", "FAIL" if bad else "PASS", "; ".join(bad))
    need_v2("goal chance not lower than v1 by more than the tolerance", chance)

    def drawdown() -> Check:
        rows_ = [(k, v1[k], r) for k, r in v2.items() if k in v1 and r["dd90"] is not None and v1[k]["dd90"] is not None]
        if not rows_:
            return Check("worst-10% drawdown not higher than v1", na, "no goal run with a drawdown")
        bad = [f"{k[0]} ({_pct(a['dd90'], 1)} -> {_pct(b['dd90'], 1)}){_v1_note(a)}" for k, a, b in rows_
               if b["dd90"] > a["dd90"] + drawdown_tie + 1e-9]
        return Check("worst-10% drawdown not higher than v1", "FAIL" if bad else "PASS", "; ".join(bad))
    need_v2("worst-10% drawdown not higher than v1", drawdown)

    def time_limit() -> Check:
        worst = max(v2.values(), key=lambda r: r["cpu_s"])
        return Check(f"heaviest persona <= {TIME_LIMIT_S:.0f} s of work (CPU seconds)",
                     "PASS" if worst["cpu_s"] <= TIME_LIMIT_S else "FAIL",
                     f"{worst['persona']}/{worst['goal']}: {worst['cpu_s']:.1f} s CPU, {worst['wall_s']:.1f} s wall")
    need_v2(f"heaviest persona <= {TIME_LIMIT_S:.0f} s of work (CPU seconds)", time_limit)

    def memory() -> Check:
        worst = max(v2.values(), key=lambda r: r["peak_mb"])
        goals = sorted({r["goal"] for r in v2.values()})
        return Check(f"peak memory <= {MEMORY_LIMIT_MB:.0f} MB", "PASS" if worst["peak_mb"] <= MEMORY_LIMIT_MB else "FAIL",
                     f"{worst['persona']}/{worst['goal']}: {worst['peak_mb']:.0f} MB (goals measured: {', '.join(goals)}; "
                     "the contract case is a 30-year goal: --goal-years 30)")
    need_v2(f"peak memory <= {MEMORY_LIMIT_MB:.0f} MB", memory)

    checks += [Check("full offline suite green (known Agent D failures only)", "MANUAL", "pytest -q -m \"not live\""),
               Check("docs updated; the report shows the method", "MANUAL", "Method and Why tabs")]
    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{c.status:<6} {c.name.ljust(width)}  {c.detail}".rstrip() for c in checks]
    failed = [c for c in checks if c.status == "FAIL"]
    inactive = any(c.status == "N/A" for c in checks)
    if failed:
        lines.append(f"\nNOT ACCEPTED: {len(failed)} check(s) failed. The flag stays v1.")
    elif inactive:
        lines.append("\nNOTHING TO ACCEPT YET: some checks cannot be measured (v2 not run or not active). The flag stays v1.")
    else:
        lines.append("\nALL MEASURABLE CHECKS PASS. The MANUAL items remain (suite, docs, report).")
    return "\n".join(lines)


def markdown_table(rows: list[dict]) -> str:
    head = ["persona", "class", "goal", "method", "holdings", "effN", "largest", "stocks", "chance", "dd90", "universe",
            "cpu s", "MB"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    v1 = _v1_rows(rows)
    for r in rows:
        if not r.get("ok"):
            out.append(f"| {r['persona']} | | {r['goal']} | {r['method']} | ERROR: {r.get('error')} |" + " |" * 8)
            continue
        if _inactive(r, v1):
            out.append("| " + " | ".join([r["persona"], r["c_level"], r["goal"], r["method"]] + ["n/a"] * (len(head) - 4)) + " |")
            continue
        label = r["method"] + ("?" if r["method"] == "v2" and not r.get("v2") else "")
        out.append("| " + " | ".join([r["persona"], r["c_level"], r["goal"], label, str(r["n_holdings"]),
                                      _num(r["eff_n"], ".1f"), _pct(r["largest"]), _pct(r["stock_share"]),
                                      _pct(r["chance"]), _pct(r["dd90"], 1), str(r["n_universe"]),
                                      _num(r["cpu_s"], ".1f"), _num(r["peak_mb"], ".0f")]) + " |")
    return "\n".join(out)


# ==========================================================================
# Command line
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--personas", default=",".join(PERSONA_ORDER), help="comma separated persona keys")
    ap.add_argument("--goals", default="none,goal", help="'none' (no goal) and/or 'goal' (the --goal-years goal)")
    ap.add_argument("--goal-years", type=float, default=15.0)
    ap.add_argument("--methods", default="v1,v2", help="v1, v2 or v1,v2")
    ap.add_argument("--jobs", type=int, default=1, help="runs in parallel (timing is cleanest with 1)")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds per run")
    ap.add_argument("--no-determinism", action="store_true", help="skip the same-input-twice rerun")
    ap.add_argument("--drawdown-tie", type=float, default=0.0,
                    help="how far (as a fraction, 0.01 = one point) a v2 worst-10%% drawdown may exceed v1's; contract: 0")
    ap.add_argument("--json", help="write the rows and checks here")
    ap.add_argument("--md", help="write the table as Markdown here")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--persona", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.worker:
        row = run_one(args.persona, args.goal_years or None)
        Path(args.out).write_text(json.dumps(row), encoding="utf-8")
        sys.stdout.flush()
        os._exit(0)                                           # a stray non-daemon thread must not keep the run alive

    personas = [p for p in args.personas.split(",") if p]
    unknown = sorted(set(personas) - set(PERSONA_ORDER))
    methods = [m for m in args.methods.split(",") if m]
    goal_flags = [g for g in args.goals.split(",") if g]
    if unknown or not set(methods) <= {"v1", "v2"} or not set(goal_flags) <= {"none", "goal"} or not methods:
        ap.error(f"unknown persona {unknown}, method or goal choice (methods v1/v2, goals none/goal)")
    goals = [None if g == "none" else args.goal_years for g in goal_flags]

    rows, repeats = run_all(personas, goals, methods, args.jobs, args.timeout, determinism=not args.no_determinism)
    print(f"Portfolio method comparison, committed Yahoo snapshot as of {AS_OF}, offline. "
          f"Goal: start amount grown at the class rate over {args.goal_years:g} years.\n")
    print(format_table(rows))
    print("\nTop holdings:\n" + format_holdings(rows))
    checks = acceptance(rows, repeats, drawdown_tie=args.drawdown_tie)
    print("\nAcceptance (docs/PORTFOLIO_V2.md section 5):\n" + format_checks(checks))
    if args.md:
        Path(args.md).write_text(markdown_table(rows) + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "repeats": repeats,
                                               "checks": [c.__dict__ for c in checks]}, indent=1), encoding="utf-8")
    return 1 if any(c.status == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
