"""State leaks between simultaneous requests: the server is ONE process with module-level caches (price memory, directory,
rate limiters, sealing key), so a build must not depend on what else is being built at that moment.

Eight different investors (every class, goals and none, an IBKR file, holdings, a Risk Game, different interests) are built ALONE and then all at
once in threads, once (CONCURRENT_ROUNDS=3 for three rounds, each in a different order); every answer must be identical to the one built alone (the
sealed contexts are opened and compared by their facts). The same for /trade-check and /profile running alongside, and
for the What-If Agent. SYNTHETIC prices, no network, no LLM. `CONCURRENT_ROUNDS` changes the number of rounds (default 1).

Not compared: the news briefing (it has a 5 s wall-clock budget and is switched off here, see PORTFOLIO_BRIEFING_SECONDS)."""

from __future__ import annotations

import copy
import json
import os
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant.advisor.context import open_context
from tests.system.test_risk_game_blend import questionnaire_side, random_answers, with_game

ROUNDS = int(os.environ.get("CONCURRENT_ROUNDS", "1"))
DATA = Path(__file__).parent.parent / "agent_b" / "data"
SEALED = ("chat_context", "briefing_context", "what_if_context", "trade_context")


def seed_for(want: str, start: int) -> int:
    return next(s for s in range(start, start + 500) if questionnaire_side(random_answers(s))[3] == want)


def game(tolerance: int, loss: int | None = None) -> dict:
    stamp = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
    return {"tolerance": tolerance, "completed_at": stamp, **({"loss_acceptance": loss} if loss is not None else {})}


def investors() -> dict[str, dict]:
    xml = (DATA / "ibkr_cautious_seller.xml").read_text(encoding="utf-8")
    csv = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    inv = {}

    def add(name, cls, seed0, g, **body):
        inv[name] = {"answers": with_game(random_answers(seed_for(cls, seed0)), g), "horizon_years": body.pop("horizon_years", 10.0), **body}

    add("gate_cash", "C1", 9000, game(20, 10), interest_areas=["cash", "government_bonds"], horizon_years=3.0)
    add("c2_goal", "C2", 9100, game(30, 25), interest_areas=["bonds", "gold"], horizon_years=12.0, goal_amount=60000.0, monthly_flow=300.0)
    add("c3_xml", "C3", 9200, None, interest_areas=["stocks", "bonds"], ibkr_report=xml, ibkr_filename="r.xml")
    add("c4_goal", "C4", 9300, game(85, 80), interest_areas=["technology", "us_stocks"], horizon_years=20.0, goal_amount=250000.0, monthly_flow=1000.0)
    add("c5_csv", "C5", 9400, game(95, 90), interest_areas=["crypto", "stocks"], horizon_years=8.0, ibkr_report=csv, ibkr_filename="r.csv")
    add("retiree", "C2", 9500, game(40), interest_areas=["dividend_stocks", "bonds"], horizon_years=25.0, goal_amount=20000.0, monthly_flow=-2000.0,
        liquidity_need=0.2)
    add("c3_holdings", "C3", 9600, game(55, 50), interest_areas=["dividend_stocks", "bonds"], current_holdings={"VTI": 0.5, "BND": 0.5})
    add("c4_wide", "C4", 9700, None, interest_areas=["stocks", "bonds", "gold", "technology", "healthcare", "energy", "real_estate"],
        horizon_years=15.0, goal_amount=120000.0, monthly_flow=500.0, min_cash_buffer=0.1)
    return inv


@pytest.fixture(scope="module")
def app():
    from main import app as real_app
    keys = ("EGOQUANT_DATA", "EGOQUANT_BRIEFING", "EGOQUANT_NEWS_WORKER", "EGOQUANT_WARM")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
    limit = egoquant_routes._PORTFOLIO_LIMIT
    egoquant_routes._PORTFOLIO_LIMIT = 10_000                       # the 20 a minute limit is not what is under test
    yield real_app
    egoquant_routes._PORTFOLIO_LIMIT = limit
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def call(app, path: str, body: dict) -> tuple[int, dict | str]:
    r = TestClient(app, raise_server_exceptions=False).post(path, json=body)      # one client per call: no shared test portal
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def normalised(status: int, body) -> dict:
    out = copy.deepcopy(body)
    if status == 200 and isinstance(out, dict):
        for key in SEALED:
            if isinstance(out.get(key), str):
                out[key] = open_context(out[key])                      # the stamp inside is not part of the facts
    return {"status": status, "body": out}


def differences(a, b, path="") -> list[str]:
    if type(a) is not type(b):
        return [f"{path}: {str(a)[:60]} vs {str(b)[:60]}"]
    if isinstance(a, dict):
        return [d for k in sorted(set(a) | set(b)) for d in (differences(a.get(k), b.get(k), f"{path}/{k}") if k in a and k in b else [f"{path}/{k}: only on one side"])]
    if isinstance(a, list):
        return [f"{path}: length {len(a)} vs {len(b)}"] if len(a) != len(b) else [d for i, (x, y) in enumerate(zip(a, b)) for d in differences(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {str(a)[:60]} vs {str(b)[:60]}"]


def run_together(app, jobs: dict, order_seed: int) -> dict:
    names = list(jobs)
    random.Random(order_seed).shuffle(names)
    results, barrier = {}, threading.Barrier(len(names))

    def work(name):
        path, body = jobs[name]
        barrier.wait()
        results[name] = normalised(*call(app, path, body))

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_eight_simultaneous_builds_equal_the_same_investors_built_alone(app):
    inv = investors()
    jobs = {name: ("/portfolio", body) for name, body in inv.items()}
    alone = {name: normalised(*call(app, "/portfolio", body)) for name, body in inv.items()}
    for name, got in alone.items():
        assert got["status"] == 200, (name, str(got["body"])[:200])
        assert got["body"]["approved"], name
    for name in ("c3_xml", "c4_goal"):                                  # a baseline: alone twice is the same, so a difference below is the concurrency
        assert not differences(alone[name], normalised(*call(app, "/portfolio", inv[name]))), f"{name} is not deterministic even alone"
    for rnd in range(ROUNDS):
        together = run_together(app, jobs, rnd)
        bad = {name: differences(alone[name], together[name])[:4] for name in inv if differences(alone[name], together[name])}
        assert not bad, f"round {rnd}: results that changed when built at the same time as others: {json.dumps(bad)[:900]}"


def test_trade_checks_and_profiles_alongside_builds_are_unchanged(app):
    inv = investors()
    report = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    jobs: dict[str, tuple[str, dict]] = {f"build:{n}": ("/portfolio", b) for n, b in list(inv.items())[:4]}
    for n in ("gate_cash", "c4_goal", "c3_holdings"):
        keep = {k: v for k, v in inv[n].items() if k not in ("current_holdings", "goal_amount", "monthly_flow", "ibkr_report", "ibkr_filename")}
        jobs[f"check:{n}"] = ("/trade-check", {**keep, "ibkr_report": report, "planned": {"ticker": "VTI", "side": "buy", "amount": 1500}})
    for n, b in inv.items():
        jobs[f"profile:{n}"] = ("/profile", b["answers"])
        jobs[f"form:{n}"] = ("/preferences/form", {"answers": b["answers"], "trading_platforms": []})
    alone = {name: normalised(*call(app, path, body)) for name, (path, body) in jobs.items()}
    assert all(r["status"] == 200 for r in alone.values()), {n: r["status"] for n, r in alone.items() if r["status"] != 200}
    together = run_together(app, jobs, 7)
    bad = {name: differences(alone[name], together[name])[:4] for name in jobs if differences(alone[name], together[name])}
    assert not bad, json.dumps(bad)[:900]


def test_simultaneous_what_if_runs_equal_the_same_runs_alone(app):
    from egoquant.agents import what_if as wi
    inv = investors()
    plan = [("c2_goal", {"extra_monthly_contribution": 400.0}), ("c4_goal", {"one_time_contribution": 20000.0}),
            ("c4_wide", {"goal_amount": 300000.0}), ("retiree", {"horizon_years": 15.0}),
            ("c4_wide", {"extra_monthly_contribution": 300.0, "mode": "reoptimize"})]
    contexts = []
    for name, scenario in plan:
        status, body = call(app, "/portfolio", inv[name])
        assert status == 200 and body["what_if_context"], name
        contexts.append((f"{name}#{len(contexts)}", body["what_if_context"], scenario))

    def dump(result):
        d = result.model_dump(mode="json")
        d.pop("timestamp", None)
        return d

    alone = {name: dump(wi.run_what_if(token, scenario)) for name, token, scenario in contexts}
    assert all(r["status"] in ("ok", "blocked") for r in alone.values()), {n: (r["status"], r.get("reason")) for n, r in alone.items()}
    results, barrier = {}, threading.Barrier(len(contexts))

    def work(name, token, scenario):
        barrier.wait()
        results[name] = dump(wi.run_what_if(token, scenario))

    threads = [threading.Thread(target=work, args=c) for c in contexts]
    [t.start() for t in threads]
    [t.join() for t in threads]
    bad = {n: differences(alone[n], results[n])[:3] for n in alone if differences(alone[n], results[n])}
    assert not bad, json.dumps(bad)[:900]
