"""What-if agent: the same scenario alone vs. 6 at once in one process (snapshot prices, no network, no LLM).
usage: whatif_par.py <worktree>"""
import json
import os
import sys
import threading
import time

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.update({"EGOQUANT_PRICE_MODE": "snapshot", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
for key in ("TIINGO_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA"):
    os.environ.pop(key, None)

from fastapi.testclient import TestClient  # noqa: E402

import egoquant_routes  # noqa: E402
from egoquant.agents import what_if as wi  # noqa: E402
from main import app  # noqa: E402
from tests.system.test_risk_game_blend import questionnaire_side, random_answers  # noqa: E402

egoquant_routes._PORTFOLIO_LIMIT = 10 ** 6
client = TestClient(app, raise_server_exceptions=False)


def seed_for(want, start):
    for s in range(start, start + 500):
        if questionnaire_side(random_answers(s))[3] == want:
            return s


plan = [("C2", 9100, {"extra_monthly_contribution": 400.0}), ("C3", 9200, {"horizon_years": 5.0, "mode": "same_portfolio"}),
        ("C4", 9300, {"one_time_contribution": 20000.0}), ("C4", 9310, {"goal_amount": 400000.0}),
        ("C5", 9400, {"extra_monthly_contribution": 1500.0, "horizon_years": 12.0}),
        ("C3", 9210, {"extra_monthly_contribution": 300.0, "mode": "reoptimize"})]
contexts = []
for cls, s0, scenario in plan:
    answers = random_answers(seed_for(cls, s0))
    body = {"answers": answers, "interest_areas": ["stocks", "bonds"], "horizon_years": 10.0, "goal_amount": answers["amount_invested"] * 3,
            "monthly_flow": 300.0}
    r = client.post("/portfolio", json=body)
    assert r.status_code == 200, r.text[:200]
    contexts.append((r.json()["what_if_context"], scenario, r.json()["c_level"]))
print("contexts built:", [c[2] for c in contexts], flush=True)


def dump(result):
    d = result.model_dump(mode="json")
    d.pop("timestamp", None)
    return d


alone = []
for token, scenario, _ in contexts:
    t0 = time.time()
    alone.append(dump(wi.run_what_if(token, scenario)))
    print("alone", alone[-1]["status"], alone[-1].get("reason"), round(time.time() - t0, 1), "s", flush=True)

wi._rate.clear() if hasattr(wi, "_rate") else None
results, barrier = {}, threading.Barrier(len(contexts))


def work(i):
    barrier.wait()
    t0 = time.time()
    results[i] = (dump(wi.run_what_if(contexts[i][0], contexts[i][1])), time.time() - t0)


threads = [threading.Thread(target=work, args=(i,)) for i in range(len(contexts))]
[t.start() for t in threads]
[t.join() for t in threads]
differ = 0
for i in range(len(contexts)):
    got, secs = results[i]
    same = got == alone[i]
    differ += not same
    print("parallel", i, got["status"], got.get("reason"), round(secs, 1), "s", "SAME" if same else "DIFFERENT")
    if not same and got["status"] == alone[i]["status"] == "ok":
        for k in got:
            if got[k] != alone[i][k]:
                print("   differs:", k, str(alone[i][k])[:120], "->", str(got[k])[:120])
print("what-if results that differ:", differ)
