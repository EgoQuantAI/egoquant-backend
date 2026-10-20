"""At most EGOQUANT_MAX_CONCURRENT_BUILDS portfolio builds run at once; the others wait their turn and, after
EGOQUANT_BUILD_WAIT_SECONDS, get a plain 503 with Retry-After. A slot is always given back, whatever the build does, and a request that
never reaches the pipeline never holds one.

Why (measured 20 Oct, damla_hostile/conc.py flood): builds share one CPU, so running more at once only adds memory (about 150 MB for a
60-year goal build; 20 at once peaked at 3.2 GB on a server with 512 MB) and waiting (206-239 s each, the proxy gives up near 250 s).

The pipeline is replaced by a stub that blocks on an event, so the tests are about the gate only. SYNTHETIC mode, no network."""

import threading
import time
from pathlib import Path
from threading import BoundedSemaphore

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant import settings
from tests.system.test_risk_game_blend import random_answers

BUSY = "The service is busy building other portfolios. Please try again in a minute."
ANSWERS = random_answers(11)
ANSWERS["amount_invested"] = 123456.78                                           # a value the 503 must never repeat
BODY = {"answers": ANSWERS, "interest_areas": ["stocks"], "horizon_years": 5.0}


@pytest.fixture
def gate(monkeypatch):
    """(app, set_slots(n), wait seconds set to 1): a stub pipeline that blocks until its own event is set, then ends in a 422 (ValueError)."""
    from main import app
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "off")
    monkeypatch.setenv("EGOQUANT_BUILD_WAIT_SECONDS", "1")
    state = {"inside": 0, "peak": 0, "lock": threading.Lock(), "release": threading.Event(), "entered": threading.Semaphore(0), "mode": "wait"}

    def pipeline(*args, **kwargs):
        with state["lock"]:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
        state["entered"].release()
        try:
            if state["mode"] == "wait":
                state["release"].wait(20)
            elif state["mode"] == "crash":
                raise RuntimeError("boom")
            raise ValueError("stub: the pipeline is not run in this test")
        finally:
            with state["lock"]:
                state["inside"] -= 1

    monkeypatch.setattr(egoquant_routes, "run_pipeline", pipeline)

    def slots(n):
        monkeypatch.setattr(egoquant_routes, "_build_slots", BoundedSemaphore(n))

    slots(3)
    egoquant_routes._portfolio_timestamps.clear()
    yield app, slots, state
    state["release"].set()
    egoquant_routes._portfolio_timestamps.clear()


def post_in_thread(app, results, key, body=BODY):
    def run():
        r = TestClient(app, raise_server_exceptions=False).post("/portfolio", json=body)
        results[key] = (r.status_code, r.json(), dict(r.headers), time.monotonic())

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_settings_defaults_minimum_and_junk(monkeypatch):
    monkeypatch.delenv("EGOQUANT_MAX_CONCURRENT_BUILDS", raising=False)
    monkeypatch.delenv("EGOQUANT_BUILD_WAIT_SECONDS", raising=False)
    assert settings.max_concurrent_builds() == 1 and settings.build_wait_seconds() == 120.0
    for raw, builds in (("1", 1), ("0", 1), ("-4", 1), ("7", 7), ("many", 1), ("", 1), ("2.5", 1)):
        monkeypatch.setenv("EGOQUANT_MAX_CONCURRENT_BUILDS", raw)
        assert settings.max_concurrent_builds() == builds, raw
    for raw, wait in (("5", 5.0), ("-1", 0.0), ("soon", 120.0), ("", 120.0)):
        monkeypatch.setenv("EGOQUANT_BUILD_WAIT_SECONDS", raw)
        assert settings.build_wait_seconds() == wait, raw
    assert egoquant_routes._build_slots._value >= 1                              # the real semaphore exists and has a slot


def test_three_slow_builds_run_together_and_a_fourth_is_queued_then_served(gate):
    app, slots, state = gate
    state["release"].clear()
    results: dict = {}
    threads = []
    for i in range(3):
        egoquant_routes._portfolio_timestamps.clear()
        threads.append(post_in_thread(app, results, i))
    for _ in range(3):
        assert state["entered"].acquire(timeout=10)                              # all three are inside the pipeline at once
    assert state["peak"] == 3
    egoquant_routes._portfolio_timestamps.clear()
    threads.append(post_in_thread(app, results, "fourth"))
    time.sleep(0.4)
    assert state["inside"] == 3 and "fourth" not in results and not state["entered"].acquire(timeout=0.01)   # queued: not inside, not answered
    state["mode"] = "end"
    state["release"].set()                                                       # the first three end; the fourth gets a slot and is served
    [t.join(20) for t in threads]
    assert sorted(map(str, results)) == ["0", "1", "2", "fourth"]
    assert {r[0] for r in results.values()} == {422}, {k: r[0] for k, r in results.items()}      # served (the stub ends in a plain 422), not 503
    assert state["peak"] == 3                                                    # the fourth never made it four at once


def test_a_fourth_that_waits_longer_than_the_limit_gets_a_503_with_retry_after(gate):
    app, slots, state = gate
    state["release"].clear()
    results: dict = {}
    holders = []
    for i in range(3):
        egoquant_routes._portfolio_timestamps.clear()
        holders.append(post_in_thread(app, results, i))
    for _ in range(3):
        assert state["entered"].acquire(timeout=10)
    egoquant_routes._portfolio_timestamps.clear()
    t0 = time.monotonic()
    late = TestClient(app, raise_server_exceptions=False).post("/portfolio", json=BODY)
    waited = time.monotonic() - t0
    assert late.status_code == 503 and 0.9 <= waited < 6, (late.status_code, waited)               # it waited the 1 s limit, then gave up
    assert late.json() == {"detail": BUSY} and late.headers["retry-after"] == "30"
    assert "123456" not in late.text and "stocks" not in late.text                                   # no value the client sent
    state["mode"] = "end"
    state["release"].set()
    [t.join(20) for t in holders]
    assert {r[0] for r in results.values()} == {422} and state["peak"] == 3


@pytest.mark.parametrize("mode", ["crash", "end"])
def test_a_slot_is_given_back_after_an_exception(gate, mode):
    app, slots, state = gate
    slots(1)
    state["mode"] = mode
    expect = 500 if mode == "crash" else 422
    for _ in range(4):                                                           # one slot, four failures in a row: never a 503
        egoquant_routes._portfolio_timestamps.clear()
        r = TestClient(app, raise_server_exceptions=False).post("/portfolio", json=BODY)
        assert r.status_code == expect, (mode, r.status_code, r.text[:100])
    assert egoquant_routes._build_slots._value == 1 and state["inside"] == 0


def test_a_request_that_fails_validation_never_waits_for_or_holds_a_slot(gate):
    app, slots, state = gate
    slots(1)
    state["release"].clear()
    results: dict = {}
    egoquant_routes._portfolio_timestamps.clear()
    holder = post_in_thread(app, results, "holder")
    assert state["entered"].acquire(timeout=10)                                  # the only slot is taken
    client = TestClient(app, raise_server_exceptions=False)
    bad_bodies = [{**BODY, "horizon_years": 0}, {**BODY, "interest_areas": []}, {**BODY, "answers": {**ANSWERS, "choices": {}}},
                  {**BODY, "answers": {**ANSWERS, "questionnaire_version": "old"}}, {"nope": 1}]
    for body in bad_bodies:
        egoquant_routes._portfolio_timestamps.clear()
        t0 = time.monotonic()
        r = client.post("/portfolio", json=body)
        assert r.status_code in (409, 422) and time.monotonic() - t0 < 0.8, (r.status_code, time.monotonic() - t0)   # answered at once
    assert egoquant_routes._build_slots._value == 0                              # still only the holder's slot is taken
    state["mode"] = "end"
    state["release"].set()
    holder.join(20)
    assert egoquant_routes._build_slots._value == 1


def test_the_trade_check_uses_the_same_gate(gate):
    """/trade-check (visitor, and with it /account/trade-check and the chat's trade tool) goes through _execute too: with the only slot taken it
    waits the limit and then gets the same 503; the what-if reoptimize calls _execute as well (agents/what_if.py)."""
    app, slots, state = gate
    slots(1)
    state["release"].clear()
    results: dict = {}
    egoquant_routes._portfolio_timestamps.clear()
    holder = post_in_thread(app, results, "holder")
    assert state["entered"].acquire(timeout=10)
    egoquant_routes._portfolio_timestamps.clear()
    csv = (Path(__file__).parent / "agent_b" / "data" / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    keep = {k: v for k, v in BODY.items() if k != "interest_areas"}
    check = TestClient(app, raise_server_exceptions=False).post("/trade-check", json={
        **keep, "interest_areas": ["stocks"], "ibkr_report": csv, "planned": {"ticker": "VTI", "side": "buy", "amount": 100}})
    assert check.status_code == 503 and check.json() == {"detail": BUSY} and check.headers["retry-after"] == "30", (check.status_code, check.text[:150])
    state["mode"] = "end"
    state["release"].set()
    holder.join(20)
    assert egoquant_routes._build_slots._value == 1
    assert "routes._execute(" in (Path(egoquant_routes.__file__).parent / "egoquant" / "agents" / "what_if.py").read_text(encoding="utf-8")
