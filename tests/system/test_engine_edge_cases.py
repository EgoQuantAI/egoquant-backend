"""Extreme but valid inputs through the real /portfolio route: each is a plain 200 or a plain 422, never a 5xx, and a 200 is always
a coherent answer.

Coherent means: every number finite and sane, weights >= 0 and summing to 1 (or no holdings at all with approved=False and a plain
headline), `approved` agreeing with the violations, the checks and the report's own verdict, the class equal to /profile's for the
same answers, an approved portfolio inside its class caps and its amounts adding up to the reference amount.

Cases: 1 USD and 1e12, needed = invested, horizons 0.1 and 60 years, goals below the start amount / 1e12 / tiny, monthly flows of
plus and minus 1e9, liquidity and cash buffer at 1.0, one and twenty interest areas, holdings that sum to 0.5 / 1.5 / 0, holdings
outside the catalog, an IBKR report with an unknown ticker. SYNTHETIC prices, no network, no LLM."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from tests.helpers import CONFIG
from tests.system.test_risk_game_blend import questionnaire_side, random_answers

AREAS20 = ["stocks", "us_stocks", "international_stocks", "emerging_markets", "china", "small_caps", "dividend_stocks", "technology",
           "healthcare", "financials", "energy", "consumer", "industrials", "utilities", "communication", "materials", "real_estate",
           "bonds", "government_bonds", "gold"]
CSV = (Path(__file__).parent.parent / "agent_b" / "data" / "ibkr_steady_holder.csv").read_text(encoding="utf-8").replace("VTI", "ZZZZ")


def seed_for(want: str, start: int) -> int:
    return next(s for s in range(start, start + 500) if questionnaire_side(random_answers(s))[3] == want)


def non_finite(text: str):
    raise ValueError(f"non-finite constant {text} in the JSON")


def scan(node, path="") -> list[str]:
    if isinstance(node, float) and not math.isfinite(node):
        return [f"{path}: not finite"]
    if isinstance(node, (int, float)) and not isinstance(node, bool) and abs(node) > 1e15:
        return [f"{path}: {node:.3g} is not a sane number"]
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in scan(v, f"{path}/{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in scan(v, f"{path}[{i}]")]
    return []


def cases(answers: dict, *, goals: bool):
    """(name, body mutation, must_be_refused)"""
    start = answers["amount_invested"]

    def a(**kw):
        return lambda b: b["answers"].update(kw)

    def top(**kw):
        return lambda b: b.update(kw)

    def both(*fns):
        return lambda b: [f(b) for f in fns] and None

    def goal(amount, flow=0.0, **kw):
        return top(goal_amount=amount, monthly_flow=flow, **kw)

    out = [("invested 1 USD", a(amount_invested=1.0, amount_needed=0.0), False),
           ("invested 0.01", a(amount_invested=0.01, amount_needed=0.0), False),
           ("invested 1e12", a(amount_invested=1e12, amount_needed=0.0), False),
           ("needed == invested", a(amount_needed=start), False),
           ("invested 1e12 == needed", a(amount_invested=1e12, amount_needed=1e12), False),
           ("horizon 0.1 years", top(horizon_years=0.1), False),
           ("horizon 60 years", top(horizon_years=60.0), False),
           ("liquidity_need 1.0", top(liquidity_need=1.0), False),
           ("min_cash_buffer 1.0", top(min_cash_buffer=1.0), False),
           ("liquidity 1.0 + min_cash 1.0", top(liquidity_need=1.0, min_cash_buffer=1.0), False),
           ("liquidity 0.5 + min_cash 0.5", top(liquidity_need=0.5, min_cash_buffer=0.5), False),
           ("liquidity 0.45", top(liquidity_need=0.45), False),
           ("one interest area", top(interest_areas=["crypto"]), False),
           ("twenty interest areas", top(interest_areas=AREAS20), False),
           ("twenty equal interest areas", top(interest_areas=["stocks"] * 20), False),
           ("no interest area", top(interest_areas=[]), True),
           ("twenty-one interest areas", top(interest_areas=AREAS20 + ["cash"]), True),
           ("an unknown interest area", top(interest_areas=["stocks", "dogecoin_moon"]), True),
           ("a holding outside the catalog", top(current_holdings={"ZZZZ": 1.0}), True),
           ("holdings sum 0.5", top(current_holdings={"VTI": 0.3, "BND": 0.2}), True),
           ("holdings sum 1.5", top(current_holdings={"VTI": 0.8, "BND": 0.7}), True),
           ("holdings that are all zero", top(current_holdings={"VTI": 0.0}), True),
           ("no holdings ({})", top(current_holdings={}), True),
           ("a negative holding", top(current_holdings={"VTI": -0.5, "BND": 1.5}), True),
           ("a holding of 1e300", top(current_holdings={"VTI": 1e300}), True),
           ("fifty holdings outside the catalog", top(current_holdings={f"ZZ{i}": 0.02 for i in range(50)}), True),
           ("fifty-one holdings", top(current_holdings={f"ZZ{i}": 0.02 for i in range(51)}), True),
           ("odd tickers as holdings", top(current_holdings={"": 0.5, "A" * 300: 0.3, "É🙂": 0.2}), True),
           ("an IBKR report with an unknown ticker", top(ibkr_report=CSV, ibkr_filename="r.csv"), False),
           ("an empty IBKR report", top(ibkr_report="", ibkr_filename="r.csv"), True),
           ("an IBKR report and holdings that sum to 1.5", top(ibkr_report=CSV, ibkr_filename="r.csv", current_holdings={"VTI": 1.5}), True)]
    if goals:
        out += [("horizon 60 years + goal", both(top(horizon_years=60.0), goal(start * 50, 500.0)), False),
                ("goal below the start amount", goal(start * 0.5), False),
                ("goal 1e12", goal(1e12), False),
                ("goal 1e12 and +1e9 a month", goal(1e12, 1e9), False),
                ("goal and -1e9 a month", goal(start * 2, -1e9), False),
                ("tiny goal and +1e9 a month", goal(1.0, 1e9), False),
                ("the whole start withdrawn in a year", both(top(horizon_years=1.0), goal(start * 0.1, -start / 12)), False),
                ("everything extreme at once", both(a(amount_invested=1e12, amount_needed=1e12),
                                                    top(horizon_years=60.0, liquidity_need=1.0, min_cash_buffer=1.0, interest_areas=AREAS20),
                                                    goal(1e12, -1e9)), False)]
    return out


@pytest.fixture(scope="module")
def client():
    from main import app
    keys = ("EGOQUANT_DATA", "EGOQUANT_BRIEFING", "EGOQUANT_NEWS_WORKER", "EGOQUANT_WARM")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
    limit = egoquant_routes._PORTFOLIO_LIMIT
    egoquant_routes._PORTFOLIO_LIMIT = 100_000
    yield TestClient(app, raise_server_exceptions=False)
    egoquant_routes._PORTFOLIO_LIMIT = limit
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def coherent(out: dict, profile_class: str | None) -> list[str]:
    problems = scan(out)
    rep = out["report"]
    weights = [h["weight"] for h in rep["allocation"]]
    if rep["allocation"]:
        if abs(sum(weights) - 1.0) > 1e-6:
            problems.append(f"weights sum to {sum(weights):.8f}")
        if min(weights) < -1e-9:
            problems.append(f"a negative weight {min(weights)}")
    else:                                                          # no portfolio: it must say so, plainly, and not be approved
        if out["approved"] or not out["violations"] or "could not build" not in rep["headline"]:
            problems.append(f"no holdings but approved={out['approved']}, violations={len(out['violations'])}, headline={rep['headline'][:60]!r}")
    if out["approved"] != (not out["violations"]) or out["approved"] != all(c["passed"] for c in out["checks"]) \
            or rep.get("risk_review_approved") != out["approved"]:
        problems.append("approved, violations, checks and the report's verdict disagree")
    if rep.get("risk_class") != out["c_level"] or (profile_class and out["c_level"] != profile_class):
        problems.append(f"class {rep.get('risk_class')}/{out['c_level']} vs /profile {profile_class}")
    if out["approved"]:
        policy = CONFIG.c_levels[out["c_level"]]
        if rep["equity_weight"] > policy.max_equity + 1e-6 or rep["crypto_weight"] > policy.max_crypto + 1e-6:
            problems.append("approved above the class caps")
        if abs(sum(h["amount"] for h in rep["allocation"]) - rep["reference_amount"]) > max(1e-6, 1e-9 * rep["reference_amount"]):
            problems.append("amounts do not add up to the reference amount")
    return problems


@pytest.mark.parametrize("who, cls, with_goals", [("gate/C1", "C1", False), ("C5", "C5", True)])
def test_extreme_valid_inputs_give_a_plain_200_or_422_and_a_coherent_answer(client, who, cls, with_goals):
    answers = random_answers(seed_for(cls, 9000))
    base = {"answers": answers, "interest_areas": ["stocks", "bonds", "gold"], "horizon_years": 10.0}
    failures = []
    for name, mutate, must_refuse in cases(answers, goals=with_goals):
        body = copy.deepcopy(base)
        mutate(body)
        egoquant_routes._portfolio_timestamps.clear()
        r = client.post("/portfolio", json=body)
        if r.status_code >= 500 or (r.status_code not in (200, 422)):
            failures.append(f"{who} / {name}: HTTP {r.status_code} {r.text[:120]}")
            continue
        if must_refuse and r.status_code != 422:
            failures.append(f"{who} / {name}: expected a refusal, got {r.status_code}")
        if r.status_code == 422:
            if "Traceback" in r.text or '"input"' in r.text:
                failures.append(f"{who} / {name}: an unclean 422 {r.text[:120]}")
            continue
        try:
            out = json.loads(r.text, parse_constant=non_finite)
        except ValueError as err:
            failures.append(f"{who} / {name}: {err}")
            continue
        profile = client.post("/profile", json=body["answers"])
        problems = coherent(out, profile.json()["c_level"] if profile.status_code == 200 else None)
        if problems:
            failures.append(f"{who} / {name}: " + "; ".join(problems))
    assert not failures, "\n".join(failures)


def test_a_cash_need_larger_than_the_class_can_hold_is_a_plain_no_portfolio_not_a_crash(client):
    """Measured on 20 Oct (snapshot prices): for C3 and C5 a cash buffer or liquidity need of 0.5 or more has no feasible portfolio. What is
    pinned is the SHAPE of that answer (200, approved False, no holdings, a plain headline and reason), not the threshold."""
    answers = random_answers(seed_for("C3", 9200))
    for field in ("min_cash_buffer", "liquidity_need"):
        for value in (0.1, 0.3, 0.45, 0.6, 1.0):
            egoquant_routes._portfolio_timestamps.clear()
            r = client.post("/portfolio", json={"answers": answers, "interest_areas": ["stocks", "bonds"], "horizon_years": 10.0, field: value})
            assert r.status_code == 200, (field, value, r.text[:120])
            out = r.json()
            assert not coherent(out, None), (field, value, coherent(out, None))
            if not out["approved"]:
                assert out["status"] == "infeasible" and out["report"]["allocation"] == [], (field, value)
                assert "No portfolio is proposed" in out["report"]["headline"]
