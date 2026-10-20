"""Engine edge cases through the real routes (in-process TestClient, committed price snapshot, no network).
usage: edge.py <worktree> [first_investor_index] ; prints one line per case and an ODD list."""
import copy
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = sys.argv[1]
ONLY = sys.argv[2] if len(sys.argv) > 2 else ""
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.update({"EGOQUANT_PRICE_MODE": "snapshot", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
for key in ("TIINGO_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA", "EGOQUANT_RISK_GAME_WEIGHT", "EGOQUANT_RISK_GAME_MAY_RAISE"):
    os.environ.pop(key, None)

from fastapi.testclient import TestClient  # noqa: E402

import egoquant_routes  # noqa: E402
from main import app  # noqa: E402
from tests.helpers import CONFIG  # noqa: E402
from tests.system.test_risk_game_blend import questionnaire_side, random_answers  # noqa: E402

egoquant_routes._PORTFOLIO_LIMIT = 10 ** 6
client = TestClient(app, raise_server_exceptions=False)
DATA = Path(ROOT) / "tests" / "agent_b" / "data"
CSV = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")


def seed_for(want, start):
    for s in range(start, start + 500):
        if questionnaire_side(random_answers(s))[3] == want:
            return s


def bad_constant(name):
    raise ValueError(f"non-finite constant {name} in the JSON")


def scan(node, path="", out=None):
    """Every number must be finite and sane (|x| < 1e15)."""
    out = [] if out is None else out
    if isinstance(node, float) and (math.isnan(node) or math.isinf(node)):
        out.append((path, "non-finite"))
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and abs(node) > 1e15:
        out.append((path, f"huge {node:.3g}"))
    elif isinstance(node, dict):
        for k, v in node.items():
            scan(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            scan(v, f"{path}[{i}]", out)
    return out


AREAS20 = ["stocks", "us_stocks", "international_stocks", "emerging_markets", "china", "small_caps", "dividend_stocks", "technology",
           "healthcare", "financials", "energy", "consumer", "industrials", "utilities", "communication", "materials", "real_estate",
           "bonds", "government_bonds", "gold"]


def cases(inv):
    """(name, mutate(body) or ('raw', text), expectation) ; expectation: 'ok' (200 or 422), '422' (must be refused)."""
    out = []

    def a(**kw):
        return lambda b: b["answers"].update(kw)

    def top(**kw):
        return lambda b: b.update(kw)

    def both(*fns):
        def run(b):
            for f in fns:
                f(b)
        return run

    goal = lambda amount, flow=0.0: top(goal_amount=amount, monthly_flow=flow)   # noqa: E731
    start = inv["answers"]["amount_invested"]
    out += [("invested 1 USD, needed 0", a(amount_invested=1.0, amount_needed=0.0), "ok"),
            ("invested 0.01", a(amount_invested=0.01, amount_needed=0.0), "ok"),
            ("invested 1e12, needed 0", a(amount_invested=1e12, amount_needed=0.0), "ok"),
            ("needed == invested", a(amount_needed=start), "ok"),
            ("invested 1e12 == needed", a(amount_invested=1e12, amount_needed=1e12), "ok"),
            ("horizon 0.1y", top(horizon_years=0.1), "ok"),
            ("horizon 60y", top(horizon_years=60.0), "ok"),
            ("horizon 0.1y + goal", both(top(horizon_years=0.1), goal(start * 2)), "ok"),
            ("horizon 60y + goal", both(top(horizon_years=60.0), goal(start * 50, 500.0)), "ok"),
            ("goal below the start amount", goal(start * 0.5), "ok"),
            ("goal == start amount", goal(start), "ok"),
            ("goal 1e12", goal(1e12), "ok"),
            ("goal 0.01", goal(0.01), "ok"),
            ("goal 1e12 + monthly +1e9", goal(1e12, 1e9), "ok"),
            ("goal + monthly -1e9", goal(start * 2, -1e9), "ok"),
            ("goal tiny + monthly +1e9", goal(1.0, 1e9), "ok"),
            ("withdraw the whole start over a year", both(top(horizon_years=1.0), goal(start * 0.1, -start / 12)), "ok"),
            ("liquidity_need 1.0", top(liquidity_need=1.0), "ok"),
            ("min_cash_buffer 1.0", top(min_cash_buffer=1.0), "ok"),
            ("liquidity 1.0 + min_cash 1.0", top(liquidity_need=1.0, min_cash_buffer=1.0), "ok"),
            ("liquidity 0.5 + min_cash 0.5", top(liquidity_need=0.5, min_cash_buffer=0.5), "ok"),
            ("liquidity 1.0 + goal", both(top(liquidity_need=1.0), goal(start * 3, 200.0)), "ok"),
            ("1 interest area", top(interest_areas=["crypto"]), "ok"),
            ("20 interest areas", top(interest_areas=AREAS20), "ok"),
            ("20 duplicate interest areas", top(interest_areas=["stocks"] * 20), "ok"),
            ("no interest area ([])", top(interest_areas=[]), "422"),
            ("21 interest areas", top(interest_areas=AREAS20 + ["cash"]), "422"),
            ("an unknown interest area", top(interest_areas=["stocks", "dogecoin_moon"]), "422"),
            ("holding not in the catalog", top(current_holdings={"ZZZZ": 1.0}), "ok"),
            ("holdings sum 0.5", top(current_holdings={"VTI": 0.3, "BND": 0.2}), "ok"),
            ("holdings sum 1.5", top(current_holdings={"VTI": 0.8, "BND": 0.7}), "ok"),
            ("holdings sum 0 (zero weight)", top(current_holdings={"VTI": 0.0}), "ok"),
            ("holdings empty {}", top(current_holdings={}), "ok"),
            ("negative holding", top(current_holdings={"VTI": -0.5, "BND": 1.5}), "ok"),
            ("holding 1e300", top(current_holdings={"VTI": 1e300}), "ok"),
            ("50 unknown holdings", top(current_holdings={f"ZZ{i}": 0.02 for i in range(50)}), "ok"),
            ("51 holdings", top(current_holdings={f"ZZ{i}": 0.02 for i in range(51)}), "422"),
            ("holding with a weird ticker", top(current_holdings={"": 0.5, "A" * 300: 0.3, "É🙂": 0.2}), "ok"),
            ("ibkr csv with an unknown ticker", top(ibkr_report=CSV.replace("VTI", "ZZZZ"), ibkr_filename="r.csv"), "ok"),
            ("ibkr report empty string", top(ibkr_report="", ibkr_filename="r.csv"), "ok"),
            ("ibkr report header only", top(ibkr_report=CSV.splitlines()[0] + "\n", ibkr_filename="r.csv"), "ok"),
            ("ibkr csv + holdings sum 1.5", top(ibkr_report=CSV, ibkr_filename="r.csv", current_holdings={"VTI": 1.5}), "ok"),
            ("everything extreme at once", both(a(amount_invested=1e12, amount_needed=1e12), top(horizon_years=60.0, liquidity_need=1.0,
                                                                                           min_cash_buffer=1.0, interest_areas=AREAS20),
                                                goal(1e12, -1e9)), "ok")]
    return out


def check(name, expectation, status, text, body, c_level_expected):
    problems = []
    if status >= 500:
        problems.append(f"5xx {status}")
    if status == 503 or status == 429:
        problems.append(f"unexpected {status}")
    if expectation == "422" and status != 422:
        problems.append(f"expected a 422, got {status}")
    if status == 200:
        try:
            out = json.loads(text, parse_constant=bad_constant)
        except ValueError as err:
            return [f"JSON with a non-finite constant: {err}"]
        problems += [f"number {p}: {w}" for p, w in scan(out)]
        rep = out["report"]
        weights = [h["weight"] for h in rep["allocation"]]
        if abs(sum(weights) - 1.0) > 1e-6:
            problems.append(f"weights sum {sum(weights):.8f}")
        if any(w < -1e-9 for w in weights):
            problems.append(f"negative weight {min(weights)}")
        passed = [c["passed"] for c in out["checks"]]
        if out["approved"] != (not out["violations"]) or out["approved"] != all(passed) or rep.get("risk_review_approved") != out["approved"]:
            problems.append(f"verdict inconsistent: approved={out['approved']} violations={len(out['violations'])} checks_passed={all(passed)} "
                            f"report={rep.get('risk_review_approved')}")
        if rep.get("risk_class") != out["c_level"] or (c_level_expected and out["c_level"] != c_level_expected):
            problems.append(f"class {rep.get('risk_class')}/{out['c_level']} vs profile {c_level_expected}")
        policy = CONFIG.c_levels[out["c_level"]]
        if out["approved"]:
            if rep["equity_weight"] > policy.max_equity + 1e-6 or rep["crypto_weight"] > policy.max_crypto + 1e-6:
                problems.append(f"approved but above the class cap (equity {rep['equity_weight']:.3f} / crypto {rep['crypto_weight']:.3f})")
            amount = sum(h["amount"] for h in rep["allocation"])
            if abs(amount - rep["reference_amount"]) > max(1e-6, 1e-9 * rep["reference_amount"]):
                problems.append(f"amounts {amount} != reference {rep['reference_amount']}")
        if not out["approved"] and out["status"] == "optimal":
            problems.append("not approved although the optimiser says optimal (info)")
    elif status == 422:
        if "Traceback" in text or "input" in text[:40]:
            problems.append("422 looks unclean")
    return problems


def main():
    investors = {"gate": seed_for("C1", 9000), "c3": seed_for("C3", 9200), "c5": seed_for("C5", 9400)}
    odd, total, t0 = [], 0, time.time()
    for iname, seed in investors.items():
        if ONLY and ONLY != iname:
            continue
        base = {"answers": random_answers(seed), "interest_areas": ["stocks", "bonds", "gold"], "horizon_years": 10.0}
        prof = client.post("/profile", json=base["answers"]).json()["c_level"]
        for name, mutate, expect in cases(base):
            body = copy.deepcopy(base)
            mutate(body)
            egoquant_routes._portfolio_timestamps.clear()
            t1 = time.time()
            r = client.post("/portfolio", json=body)
            pr = client.post("/profile", json=body["answers"])
            problems = check(name, expect, r.status_code, r.text, body, pr.json()["c_level"] if pr.status_code == 200 else None)
            total += 1
            tag = "ODD " if problems else "ok  "
            extra = ""
            if r.status_code == 200:
                o = r.json()
                extra = f"{o['c_level']} approved={o['approved']} st={o['status']} n={len(o['report']['allocation'])} cash={o['report']['cash_weight']:.2f}"
                chance = (o["report"].get("goal") or {}).get("chance")
                if chance:
                    extra += f" chance={chance}"
            else:
                extra = r.text[:110].replace("\n", " ")
            print(f"{tag}{iname:5} {name[:44]:44} {r.status_code} {time.time() - t1:5.1f}s {extra}", flush=True)
            if problems:
                odd.append((iname, name, r.status_code, problems))
    print(f"\n{total} cases in {time.time() - t0:.0f}s; ODD: {len(odd)}")
    for o in odd:
        print(json.dumps(o)[:500])


main()
