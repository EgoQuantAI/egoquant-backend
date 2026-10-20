"""Concurrency and state-leak check against a LOCAL server (serve.py) on the committed price snapshot.
usage: conc.py <worktree> <port> <phase...>   phases: alone rounds mixed leak
Prints JSON lines; a final DIFFS section lists every difference between a concurrent result and the same investor alone."""
import copy
import ctypes
import json
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import requests

ROOT, PORT = sys.argv[1], int(sys.argv[2])
PHASES = sys.argv[3:] or ["alone", "rounds"]
HERE = Path(__file__).parent
sys.path.insert(0, ROOT)
os.chdir(ROOT)
SECRET = secrets.token_hex(16)                       # a throwaway for this run only, never written anywhere
os.environ["EGOQUANT_CONTEXT_SECRET"] = SECRET
os.environ["HARNESS_PORTFOLIO_LIMIT"] = "100000"

from egoquant.advisor.context import open_context  # noqa: E402
from tests.system.test_risk_game_blend import questionnaire_side, random_answers, with_game  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}"
DATA = Path(ROOT) / "tests" / "agent_b" / "data"
NOW = "2026-10-19T08:00:00+00:00"


# ------------------------------------------------------------------ memory of the server process (Windows)
_REAL: dict = {}


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def real_pid(launcher: int) -> int:
    """The venv's python.exe is a stub that starts the real interpreter as a child: measure the child."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={launcher}' | Select-Object -First 1).ProcessId"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else launcher


def memory(pid: int) -> dict:
    pid = _REAL.setdefault(pid, real_pid(pid))
    handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)      # QUERY_INFORMATION | VM_READ
    counters = PMC()
    counters.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    ctypes.windll.kernel32.CloseHandle(handle)
    mb = 1 / 1048576
    return {"rss_mb": round(counters.WorkingSetSize * mb), "peak_rss_mb": round(counters.PeakWorkingSetSize * mb),
            "commit_mb": round(counters.PagefileUsage * mb), "peak_commit_mb": round(counters.PeakPagefileUsage * mb)}


# ------------------------------------------------------------------ the server
def start_server():
    proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), ROOT, str(PORT)], env=os.environ.copy(),
                            stdout=open(Path(tempfile.gettempdir()) / "egoquant_stress_server.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                return proc
        except requests.RequestException:
            time.sleep(1)
    proc.kill()
    raise SystemExit("server did not start; see egoquant_stress_server.log in the temp folder")


# ------------------------------------------------------------------ the investors
def seed_for(want: str, start: int) -> int:
    for s in range(start, start + 400):
        if questionnaire_side(random_answers(s))[3] == want:
            return s
    raise SystemExit(f"no seed for {want}")


def game(tolerance, loss=None, crash=None):
    g = {"tolerance": tolerance, "completed_at": NOW}
    if loss is not None:
        g["loss_acceptance"] = loss
    if crash:
        g["crash_test"] = crash
    return g


def investors() -> dict:
    ibkr_csv = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    ibkr_xml = (DATA / "ibkr_cautious_seller.xml").read_text(encoding="utf-8")
    inv = {}

    def add(name, cls, seed0, g, **extra):
        a = random_answers(seed_for(cls, seed0))
        inv[name] = {"answers": with_game(a, g), "horizon_years": extra.pop("horizon_years", 10.0), **extra}

    add("gate_cash", "C1", 9000, game(20, 10), interest_areas=["cash", "government_bonds"], horizon_years=3.0)
    add("c2_goal", "C2", 9100, game(30, 25), interest_areas=["bonds", "gold"], horizon_years=12.0, goal_amount=60000.0, monthly_flow=300.0)
    add("c3_ibkr_xml", "C3", 9200, None, interest_areas=["stocks", "bonds"], ibkr_report=ibkr_xml, ibkr_filename="r.xml")
    add("c4_growth_goal", "C4", 9300, game(85, 80), interest_areas=["technology", "us_stocks", "international_stocks"],
        horizon_years=20.0, goal_amount=250000.0, monthly_flow=1000.0)
    add("c5_crypto_csv", "C5", 9400, game(95, 90, {"sold_at_drawdown": None, "sold_fraction": 0.0, "max_drawdown_seen": 0.4}),
        interest_areas=["crypto", "altcoins", "stocks"], horizon_years=8.0, ibkr_report=ibkr_csv, ibkr_filename="r.csv")
    add("retiree_withdraw", "C2", 9500, game(40), interest_areas=["dividend_stocks", "bonds"], horizon_years=25.0, goal_amount=20000.0,
        monthly_flow=-2000.0, liquidity_need=0.2)
    add("c3_holdings", "C3", 9600, game(55, 50), interest_areas=["dividend_stocks", "bonds"], current_holdings={"VTI": 0.5, "BND": 0.5})
    add("c4_wide", "C4", 9700, None, interest_areas=["stocks", "bonds", "gold", "technology", "healthcare", "energy", "emerging_markets",
                                                     "real_estate"], horizon_years=15.0, goal_amount=120000.0, monthly_flow=500.0,
        min_cash_buffer=0.1)
    return inv


# ------------------------------------------------------------------ calls and comparison
def call(path, body, timeout=900):
    t0 = time.time()
    r = requests.post(BASE + path, json=body, timeout=timeout)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text), time.time() - t0


def opened(token):
    try:
        facts = open_context(token)
        return facts
    except Exception as err:                                   # noqa: BLE001
        return {"_cannot_open": type(err).__name__}


def normalise(path, status, body):
    """The response with each sealed context replaced by its opened facts (the stamp is not part of the facts)."""
    if status != 200 or not isinstance(body, dict):
        return {"status": status, "body": body}
    out = copy.deepcopy(body)
    for key in ("chat_context", "briefing_context", "what_if_context", "trade_context"):
        if isinstance(out.get(key), str) and out.get(key):
            out[key] = opened(out[key])
    return {"status": status, "body": out}


def diff(a, b, path="", out=None, limit=25):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append((path, repr(a)[:80], repr(b)[:80]))
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append((f"{path}/{k}", "missing" if k not in a else "present", "missing" if k not in b else "present"))
            else:
                diff(a[k], b[k], f"{path}/{k}", out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((path, f"len {len(a)}", f"len {len(b)}"))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append((path, repr(a)[:80], repr(b)[:80]))
    return out


def headline(name, status, body, secs):
    if status != 200:
        return {"name": name, "status": status, "secs": round(secs, 1), "body": str(body)[:200]}
    rep = body["report"]
    goal = body.get("goal") or {}
    return {"name": name, "status": status, "secs": round(secs, 1), "c": body["c_level"], "approved": body["approved"],
            "hash": rep.get("input_hash"), "chance": (rep.get("goal") or {}).get("chance") or goal.get("chance_of_goal"),
            "n": len(rep["allocation"]), "src": body["data_source"]}


def say(**kw):
    print(json.dumps(kw, default=str), flush=True)


# ------------------------------------------------------------------ phases
def main():
    proc = start_server()
    inv = investors()
    say(event="server up", pid=proc.pid, mem=memory(proc.pid), n_investors=len(inv))
    alone, baseline_diffs = {}, {}
    try:
        if "alone" in PHASES or "rounds" in PHASES or "mixed" in PHASES:
            say(event="phase", name="alone x2 (determinism baseline)")
            for name, body in inv.items():
                runs = []
                for _ in range(2):
                    status, resp, secs = call("/portfolio", body)
                    runs.append((normalise("/portfolio", status, resp), secs, status, resp))
                alone[name] = runs[0][0]
                baseline_diffs[name] = diff(runs[0][0], runs[1][0])
                say(**headline(name, runs[0][2], runs[0][3], runs[0][1]), baseline_diffs=len(baseline_diffs[name]))
            volatile = sorted({d[0] for v in baseline_diffs.values() for d in v})
            say(event="alone-vs-alone differences (volatile fields)", paths=volatile[:30], mem=memory(proc.pid))
        if "rounds" in PHASES:
            for rnd in range(3):
                say(event="phase", name=f"concurrent round {rnd + 1}: {len(inv)} threads")
                results, order = {}, list(inv)
                random.Random(rnd).shuffle(order)
                barrier = threading.Barrier(len(order))

                def work(name):
                    barrier.wait()
                    results[name] = call("/portfolio", inv[name])

                threads = [threading.Thread(target=work, args=(n,)) for n in order]
                t0 = time.time()
                [t.start() for t in threads]
                [t.join() for t in threads]
                wall = time.time() - t0
                bad = 0
                for name in inv:
                    status, resp, secs = results[name]
                    d = [x for x in diff(alone[name], normalise("/portfolio", status, resp)) if x[0] not in
                         {v for v in volatile}]
                    bad += bool(d)
                    say(**headline(name, status, resp, secs), differs_from_alone=len(d), diffs=d[:25])
                say(event="round done", wall=round(wall, 1), slowest=round(max(r[2] for r in results.values()), 1), mem=memory(proc.pid),
                    investors_that_differ=bad)
        if "mixed" in PHASES:
            say(event="phase", name="mixed: /portfolio + visitor /trade-check + /profile + /preferences/form in parallel")
            report = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
            checks = {}
            for name in ("c3_holdings", "c4_growth_goal", "gate_cash"):
                body = {k: v for k, v in inv[name].items() if k not in ("current_holdings", "goal_amount", "monthly_flow", "ibkr_report", "ibkr_filename")}
                body = {**body, "ibkr_report": report, "planned": {"ticker": "VTI", "side": "buy", "amount": 1500}}
                st, resp, secs = call("/trade-check", body)
                checks[name] = (body, normalise("/trade-check", st, resp))
                say(event="trade-check alone", name=name, status=st, secs=round(secs, 1))
            jobs = [("/portfolio", n, inv[n]) for n in inv] + [("/trade-check", n, checks[n][0]) for n in checks] + \
                   [("/profile", n, inv[n]["answers"]) for n in inv]
            alone_small = {}
            for path, n, body in jobs:
                if path == "/profile":
                    st, resp, secs = call(path, body)
                    alone_small[(path, n)] = normalise(path, st, resp)
            results = {}
            barrier = threading.Barrier(len(jobs))

            def work2(job):
                barrier.wait()
                results[(job[0], job[1])] = call(job[0], job[2])

            threads = [threading.Thread(target=work2, args=(j,)) for j in jobs]
            [t.start() for t in threads]
            [t.join() for t in threads]
            bad = 0
            for (path, n), (st, resp, secs) in results.items():
                ref = alone[n] if path == "/portfolio" else checks[n][1] if path == "/trade-check" else alone_small[(path, n)]
                d = [x for x in diff(ref, normalise(path, st, resp)) if x[0] not in set(volatile)]
                bad += bool(d)
                if d or st != 200:
                    say(event="MIXED DIFF", path=path, name=n, status=st, diffs=d[:25])
            say(event="mixed done", jobs=len(jobs), differ=bad, mem=memory(proc.pid))
        if "flood" in PHASES:
            say(event="phase", name="flood: 20 heavy builds at once (the real limit is 20 per minute) while polling /health and /profile")
            heavy = {**inv["c4_wide"], "horizon_years": 60.0, "goal_amount": 2_000_000.0, "monthly_flow": 500.0,
                     "interest_areas": ["stocks", "bonds", "gold", "technology", "healthcare", "energy", "emerging_markets", "real_estate"]}
            probe = {"stop": False, "health": [], "profile": []}

            def poll():
                while not probe["stop"]:
                    for name, fn in (("health", lambda: requests.get(f"{BASE}/health", timeout=120)),
                                     ("profile", lambda: requests.post(f"{BASE}/profile", json=inv["c3_holdings"]["answers"], timeout=120))):
                        t0 = time.time()
                        try:
                            code = fn().status_code
                        except requests.RequestException as err:
                            code = type(err).__name__
                        probe[name].append((round(time.time() - t0, 2), code))
                    time.sleep(0.5)

            poller = threading.Thread(target=poll)
            poller.start()
            NF = int(os.environ.get("FLOOD_N", "20"))
            results, barrier = {}, threading.Barrier(NF)

            def work3(i):
                barrier.wait()
                results[i] = call("/portfolio", heavy)

            threads = [threading.Thread(target=work3, args=(i,)) for i in range(NF)]
            t0 = time.time()
            [t.start() for t in threads]
            [t.join() for t in threads]
            wall = time.time() - t0
            probe["stop"] = True
            poller.join()
            codes = Counter(r[0] for r in results.values()) if False else sorted({r[0] for r in results.values()})
            secs = sorted(round(r[2], 1) for r in results.values())
            hs = [x[0] for x in probe["health"]]
            ps = [x[0] for x in probe["profile"]]
            variants = {}
            for i, r in results.items():
                variants.setdefault(json.dumps(normalise("/portfolio", r[0], r[1]), sort_keys=True, default=str), []).append(i)
            if len(variants) > 1:
                keys = list(variants)
                ref = json.loads(keys[0])
                for k in keys[1:]:
                    say(event="FLOOD VARIANT", n_in_variant=len(variants[k]), n_in_first=len(variants[keys[0]]), diffs=diff(ref, json.loads(k))[:12])
            say(event="flood done", wall=round(wall, 1), statuses=codes, build_secs_min=secs[0], build_secs_median=secs[len(secs) // 2], build_secs_max=secs[-1],
                identical_results=len({json.dumps(normalise("/portfolio", r[0], r[1]), sort_keys=True, default=str) for r in results.values()}) == 1,
                health_n=len(hs), health_median=sorted(hs)[len(hs) // 2] if hs else None, health_max=max(hs) if hs else None,
                health_codes=sorted({str(x[1]) for x in probe["health"]}), profile_median=sorted(ps)[len(ps) // 2] if ps else None,
                profile_max=max(ps) if ps else None, mem=memory(proc.pid))
        if "leak" in PHASES:
            say(event="phase", name="30 sequential builds in one process")
            names = list(inv)
            series = [memory(proc.pid)["rss_mb"]]
            for i in range(30):
                name = names[i % len(names)]
                status, resp, secs = call("/portfolio", inv[name])
                series.append(memory(proc.pid)["rss_mb"])
                say(event="seq", i=i + 1, name=name, status=status, secs=round(secs, 1), rss_mb=series[-1])
            half = len(series) // 2
            say(event="leak summary", start=series[0], after5=series[5], after15=series[15], end=series[-1], max=max(series),
                slope_mb_per_build_second_half=round((series[-1] - series[half]) / (len(series) - 1 - half), 2), mem=memory(proc.pid))
    finally:
        say(event="final memory", mem=memory(proc.pid))
        proc.terminate()


main()
