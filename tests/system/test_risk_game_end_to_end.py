"""The Risk Game blend end to end through POST /portfolio (not only /profile), with the averaged capacity cap.

Random investors (the generators of test_risk_game_blend: boldness and capacity dials, the no-loss gate, a game for 85%), each with
random interest areas and horizon. For every one: no 5xx, approved by the Risk Engine, Agent A optimal, c_level == /profile c_level
== the class of the contract's rule, report.risk_class the same, report.category_basis present exactly when a game was sent (and
then it carries the profile's own numbers, with its sentence among the reasons), weights sum to 1, nothing on offer above the class,
sealed contexts present. Every fifth game investor is built again WITHOUT the game: the questionnaire class, no category_basis.

`RISK_GAME_E2E_N` changes the number of investors (default 30; 100 for the full check, about 2 minutes). SYNTHETIC prices, no network,
no LLM. Skipped until ProfileOut.game_played exists (docs/RISK_GAME_BLEND.md).
"""

from __future__ import annotations

import os
import random
from collections import Counter

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from tests.system.test_risk_game_blend import (RAISE_ENV, WEIGHT_ENV, blended_level, expected_class, game_level, game_score,
                                               needs_blend, number, questionnaire_side, random_answers, random_game, with_game)

N = int(os.environ.get("RISK_GAME_E2E_N", "30"))
FIRST_SEED = 7000                                                  # not the sweep's investors
AREAS = ["stocks", "us_stocks", "international_stocks", "emerging_markets", "dividend_stocks", "technology", "healthcare", "energy",
         "bonds", "government_bonds", "corporate_bonds", "inflation_protected", "gold", "commodities", "crypto", "cash", "real_estate"]

pytestmark = needs_blend


@pytest.fixture(scope="module")
def client():
    from main import app
    keys = ("EGOQUANT_DATA", "EGOQUANT_BRIEFING", "EGOQUANT_NEWS_WORKER", "EGOQUANT_WARM", WEIGHT_ENV, RAISE_ENV)
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
    os.environ.pop(WEIGHT_ENV, None)
    os.environ.pop(RAISE_ENV, None)
    yield TestClient(app, raise_server_exceptions=False)
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def post(client, path, body):
    egoquant_routes._portfolio_timestamps.clear()
    egoquant_routes._debug_timestamps.clear()
    return client.post(path, json=body)


def build(client, answers, game, areas, horizon):
    return post(client, "/portfolio", {"answers": with_game(answers, game), "interest_areas": areas, "horizon_years": horizon})


def test_every_investor_builds_approved_with_the_profiles_class_and_a_basis_only_with_a_game(client):
    problems, seen = [], Counter()
    for n in range(N):
        i = FIRST_SEED + n
        rng = random.Random(f"areas-{i}")
        answers, game = random_answers(i), random_game(i)
        areas, horizon = rng.sample(AREAS, rng.randint(1, 4)), rng.choice([3.0, 5.0, 10.0, 20.0])
        t, cap, gate, base = questionnaire_side(answers)
        level = None if game is None else game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
        want = f"C{expected_class(t, cap, gate, level)}"
        who = f"investor {i} (t={t} cap={cap} gate={gate} game_level={level} questionnaire={base} want={want} areas={areas} h={horizon})"

        profile = post(client, "/profile", with_game(answers, game))
        assert profile.status_code == 200, (who, profile.text[:200])
        p = profile.json()
        r = build(client, answers, game, areas, horizon)
        if r.status_code != 200:
            problems.append(f"{who}: /portfolio {r.status_code} {r.text[:200]}")
            continue
        out, rep = r.json(), r.json()["report"]
        found = []
        if p["c_level"] != want:
            found.append(f"/profile says {p['c_level']}")
        if out["c_level"] != p["c_level"]:
            found.append(f"/portfolio c_level {out['c_level']} != /profile {p['c_level']}")
        if rep.get("risk_class") != out["c_level"]:
            found.append(f"report.risk_class {rep.get('risk_class')}")
        if not out["approved"]:
            found.append(f"not approved: {out['violations'][:3]}")
        if out["status"] != "optimal":
            found.append(f"Agent A status {out['status']}")
        if ("category_basis" in rep) != (game is not None):
            found.append(f"category_basis present={'category_basis' in rep} with game={game is not None}")
        sentence = "whatever the Risk Game said" if gate else "Risk Game you played"         # the gate has its own sentence
        if (sentence in out["html"]) != (game is not None) or (game is None and "Risk Game you played" in out["html"]):
            found.append("the basis sentence in the HTML does not match whether a game was sent")
        if game is not None and "category_basis" in rep:
            basis = rep["category_basis"]
            expect = {"questionnaire_level": t, "game_level": level, "blended_level": blended_level(t, level), "capacity_level": cap,
                      "questionnaire_class": base, "final": out["c_level"]}
            if {k: basis.get(k) for k in expect} != expect:
                found.append(f"category_basis {basis}")
            if basis.get("binding") != p["binding_dimension"]:
                found.append(f"category_basis.binding {basis.get('binding')} != profile {p['binding_dimension']}")
            if not any(x.get("text") == basis.get("text") for x in rep.get("reasons", [])):
                found.append("the basis sentence is not among the reasons")
        if abs(sum(h["weight"] for h in rep["allocation"]) - 1.0) > 1e-6:
            found.append("weights do not sum to 1")
        if any(inst["r_level"] > p["max_r_level"] for inst in out["universe"]["instruments"]):
            found.append("a product above the class on offer")
        for key in ("chat_context", "what_if_context"):
            if not out.get(key):
                found.append(f"no {key}")
        if out.get("goal_unavailable") or "Traceback" in r.text:
            found.append("goal_unavailable / Traceback in the response")

        if game is not None and n % 5 == 0:                         # the same answers without the game
            plain = build(client, answers, None, areas, horizon)
            if plain.status_code != 200 or plain.json()["c_level"] != base or "category_basis" in plain.json()["report"] \
                    or not plain.json()["approved"]:
                found.append(f"without the game: {plain.status_code} {plain.json().get('c_level') if plain.status_code == 200 else ''}")
            seen["paired"] += 1
        seen["game" if game is not None else "no_game"] += 1
        seen[out["c_level"]] += 1
        seen["gate"] += gate
        seen["moved"] += game is not None and number(out["c_level"]) != number(base)
        if found:
            problems.append(f"{who}: " + "; ".join(found))
    assert not problems, f"{len(problems)} of {N}:\n" + "\n".join(problems[:10])
    if N >= 30:                                                     # a real mix
        assert seen["game"] >= 10 and seen["no_game"] >= 1 and seen["moved"] >= N // 15 and seen["paired"] >= 2, seen
        assert sum(1 for k in ("C1", "C2", "C3", "C4", "C5") if seen[k]) >= 3, seen


@pytest.mark.parametrize("name, crash", [
    ("sold more than the fall seen", {"sold_at_drawdown": 0.35, "sold_fraction": 0.5, "max_drawdown_seen": 0.2}),
    ("sold a share but no drawdown", {"sold_at_drawdown": None, "sold_fraction": 0.5, "max_drawdown_seen": 0.35}),
])
def test_an_impossible_crash_test_is_refused_by_portfolio_cleanly(client, name, crash):
    """/profile and /preferences/form do not look at the crash test's consistency (they answer 200); /portfolio does, with a clear 422.
    What must hold: a 4xx with the reason, never a 5xx or a traceback, and the numbers sent are not echoed."""
    answers = random_answers(7000)
    game = {"tolerance": 61, "completed_at": "2026-10-19T10:00:00+00:00", "crash_test": crash}
    r = build(client, answers, game, ["stocks"], 5.0)
    assert r.status_code == 422, (name, r.status_code, r.text[:200])
    assert "crash_test" in r.text and "Traceback" not in r.text and "0.35" not in r.text and "0.2" not in r.text, r.text[:300]
