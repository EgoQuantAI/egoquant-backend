"""The Risk Game inside the risk category (docs/RISK_GAME_BLEND.md): the blend rule, hostile game inputs, sealed contexts.

  1. the RULE as a small oracle written straight from the contract (these tests always run);
  2. a 300-investor sweep through the REAL endpoints (/profile, /preferences/form, /preferences/areas, /portfolio, /trade-check):
     class == min(cap, clamp(round((2*t + game_level)/3))), never above the cap, the gate forces C1, deterministic, the same
     class on every endpoint, eligibility and limits follow the BLENDED class, MAY_RAISE off only lowers, WEIGHT=0 = no game;
  3. hostile `answers.risk_game`: no 5xx, no echo of what was sent in a 422, a game in two places never silently wins at the top level;
  4. sealed contexts (chat, what-if, trade) still verify and carry the blended class; a replayed game changes the class only for
     builds made after it.

Parts 2 to 4 are SKIPPED, with the reason below, until the new fields exist in this tree (ProfileOut.game_played). SYNTHETIC prices,
no network, no LLM. `RISK_GAME_SWEEP_N` changes the size of the sweep (default 300).
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant.advisor.context import ContextError, open_context
from egoquant.risk_profile import build_risk_profile
from tests.helpers import ASSESSED_AT, BANK, CONFIG

N_SWEEP = int(os.environ.get("RISK_GAME_SWEEP_N", "300"))
NEW_FIELDS = "game_played" in egoquant_routes.ProfileOut.model_fields
needs_blend = pytest.mark.skipif(not NEW_FIELDS, reason="the blended-category fields (ProfileOut.game_played) are not in this "
                                                         "tree yet; see docs/RISK_GAME_BLEND.md")
WEIGHT_ENV, RAISE_ENV = "EGOQUANT_RISK_GAME_WEIGHT", "EGOQUANT_RISK_GAME_MAY_RAISE"
DATA = Path(__file__).parent.parent / "agent_b" / "data"


# ====================================================================================== 1. the rule, from the contract
def game_score(tolerance: float, loss_acceptance: float | None) -> float:
    return tolerance if loss_acceptance is None else (tolerance + loss_acceptance) / 2.0


def game_level(score: float) -> int:
    return min(5, 1 + int(score // 20))


def blended_level(t: int, level: int | None, weight: float = 1.0 / 3.0) -> int:
    blend = t if level is None else (1.0 - weight) * t + weight * level
    return max(1, min(5, int(math.floor(blend + 0.5))))                 # no ties with integer levels at weight 1/3


def expected_class(t: int, cap: int, gate: bool, level: int | None, *, may_raise: bool = True, weight: float = 1.0 / 3.0) -> int:
    if gate:
        return 1
    blended = blended_level(t, level, weight)
    return min(cap, blended) if may_raise else min(cap, t, blended)


@pytest.mark.parametrize("score, level", [(0, 1), (19.99, 1), (20, 2), (39.99, 2), (40, 3), (59.99, 3), (60, 4), (79.99, 4),
                                          (80, 5), (100, 5)])
def test_game_level_bands(score, level):
    assert game_level(score) == level


def test_game_score_is_the_mean_of_tolerance_and_loss_acceptance_or_tolerance_alone():
    assert game_score(40, None) == 40 and game_score(40, 80) == 60 and game_level(game_score(40, 80)) == 4


@pytest.mark.parametrize("t, level, cap, gate, may_raise, klass", [
    (4, 2, 5, False, True, 3), (3, 5, 5, False, True, 4), (3, 5, 5, False, False, 3), (5, 1, 5, False, True, 4),
    (5, 5, 3, False, True, 3), (2, 5, 5, False, True, 3), (2, 5, 5, False, False, 2), (1, 5, 5, False, True, 2),
    (3, None, 5, False, True, 3), (4, 5, 2, True, True, 1), (1, 1, 5, True, False, 1)])
def test_the_contracts_example_table(t, level, cap, gate, may_raise, klass):
    assert expected_class(t, cap, gate, level, may_raise=may_raise) == klass


def test_the_rule_properties_over_every_combination():
    for t in range(1, 6):
        for cap in range(1, 6):
            for level in [None, 1, 2, 3, 4, 5]:
                c = expected_class(t, cap, False, level)
                assert 1 <= c <= cap                                              # never above the cap
                assert expected_class(t, cap, False, level, may_raise=False) <= min(c, t)     # MAY_RAISE off only lowers
                if level is None:
                    assert c == min(cap, t)                                       # no game = the questionnaire
                if level == t:
                    assert c == min(cap, t)                                       # equal levels give that level
                if level is not None and level < 5:                               # monotone in the game level
                    assert expected_class(t, cap, False, level + 1) >= c
                assert expected_class(t, cap, True, level) == 1                   # the gate wins over everything
                assert expected_class(t, cap, False, level, weight=0.0) == min(cap, t)    # weight 0 = no game


# ====================================================================================== generators
def random_answers(seed: int) -> dict:
    """A valid random questionnaire as the API body (the same seed is the same investor). Two latent dials spread the
    investors over every class: boldness (the Grable-Lytton items) and capacity (horizon, savings, share, income, B3),
    and about 1 in 14 says "no loss at all" (the gate)."""
    rng = random.Random(f"answers-{seed}")
    bold, room = rng.random(), rng.random()

    def pick(question, dial, reverse=False):
        ids = [o.id for o in question.options if o.id != "refuse"]
        spot = min(1.0, max(0.0, (1.0 - dial if reverse else dial) + rng.gauss(0.0, 0.18)))
        return ids[round(spot * (len(ids) - 1))]

    choices = {}
    for q in BANK.questions:
        if q.id in ("B3_invested", "B3_needed", "K4"):
            continue
        if q.id.startswith("GL"):
            choices[q.id] = pick(q, bold, reverse=(q.id == "GL1"))        # GL1 is scored the other way round
        elif q.id in ("B2", "B4", "B5", "B6"):
            choices[q.id] = pick(q, room)
        else:
            choices[q.id] = rng.choice([o.id for o in q.options if o.id != "refuse"])
    choices["D1"] = "no" if rng.random() < 1 / 14 else "yes"
    k4 = [o.id for o in BANK.get("K4").options]
    picked = rng.sample(k4, rng.randint(1, 3))
    if "none" in picked:
        picked = ["none"]
    invested = round(10 ** rng.uniform(3.3, 6.3), 2)
    needed_share = (0.99, 0.93, 0.85, 0.7, 0.3, 0.0)[min(5, int(room * 6 + rng.gauss(0.0, 0.7)) % 6)]
    return {"questionnaire_version": BANK.questionnaire_version, "choices": choices, "multi_choices": {"K4": picked},
            "amount_invested": invested, "amount_needed": round(invested * needed_share, 2)}


def random_game(seed: int) -> dict | None:
    rng = random.Random(f"game-{seed}")
    if rng.random() < 0.15:
        return None                                                               # nobody played
    game = {"tolerance": rng.randint(0, 100),
            "completed_at": (datetime.now(timezone.utc) - timedelta(minutes=rng.choice([1, 30, 600, 3000, 60 * 24 * 90]))).isoformat()}
    if rng.random() < 0.7:
        game["loss_acceptance"] = rng.randint(0, 100)
    if rng.random() < 0.3:
        game["crash_test"] = {"sold_at_drawdown": rng.choice([None, 0.1, 0.2, 0.35]), "sold_fraction": rng.choice([0.0, 0.5, 1.0]),
                              "max_drawdown_seen": rng.choice([0.2, 0.35, 0.5])}
        crash = game["crash_test"]                                                  # a crash test that could have been played:
        if crash["sold_at_drawdown"] is None:
            crash["sold_fraction"] = 0.0                                            # nothing sold, so no fraction sold
        else:
            crash["max_drawdown_seen"] = max(crash["max_drawdown_seen"], crash["sold_at_drawdown"])   # sold within the fall seen
    return game


def with_game(answers: dict, game: dict | None) -> dict:
    return {**answers, "risk_game": game} if game is not None else dict(answers)


def questionnaire_side(answers: dict):
    """t, cap, gate and the class of the questionnaire ALONE, straight from the library (no endpoint, no game)."""
    from egoquant.risk_profile.models import Answers
    profile = build_risk_profile(Answers(questionnaire_version=answers["questionnaire_version"], choices=answers["choices"],
                                         multi_choices=answers["multi_choices"], amount_invested=answers["amount_invested"],
                                         amount_needed=answers["amount_needed"]), BANK, CONFIG, assessed_at=ASSESSED_AT)
    return profile.tolerance_level, profile.capacity_level, profile.no_loss_gate, profile.c_level


def number(c_level: str) -> int:
    return int(c_level[1:])


def test_the_generators_are_deterministic_and_valid():
    def without_time(game):
        return None if game is None else {k: v for k, v in game.items() if k != "completed_at"}      # the stamp is "minutes ago"
    assert random_answers(3) == random_answers(3)
    assert all(without_time(random_game(s)) == without_time(random_game(s)) for s in range(30))
    sides = [questionnaire_side(random_answers(s)) for s in range(N_SWEEP)]
    assert {side[3] for side in sides} == {"C1", "C2", "C3", "C4", "C5"}, {side[3] for side in sides}   # every class occurs
    assert any(side[2] for side in sides) and not all(side[2] for side in sides)                    # the gate occurs, not always
    assert {side[0] for side in sides} == {1, 2, 3, 4, 5} and {side[1] for side in sides} == {1, 2, 3, 4, 5}   # t and cap, 1-5


# ====================================================================================== the client
@pytest.fixture(scope="module")
def client():
    from main import app
    old = {k: os.environ.get(k) for k in ("EGOQUANT_DATA", "EGOQUANT_BRIEFING", "EGOQUANT_NEWS_WORKER", WEIGHT_ENV, RAISE_ENV)}
    os.environ.update({"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off"})
    os.environ.pop(WEIGHT_ENV, None)
    os.environ.pop(RAISE_ENV, None)
    yield TestClient(app, raise_server_exceptions=False)
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    egoquant_routes._portfolio_timestamps.clear()
    egoquant_routes._debug_timestamps.clear()
    yield


def post(client, path, body, **kw):
    egoquant_routes._portfolio_timestamps.clear()
    return client.post(path, json=body, **kw)


def portfolio_body(answers: dict, game: dict | None = None, **extra) -> dict:
    return {"answers": with_game(answers, game), "interest_areas": ["stocks", "bonds"], "horizon_years": 10.0, **extra}


# ====================================================================================== 2. the sweep
@needs_blend
def test_the_sweep_class_equals_min_of_cap_and_blend_on_every_endpoint(client):
    seen = {"classes": set(), "gate": 0, "lowered": 0, "raised": 0, "capped": 0, "no_game": 0}
    for i in range(N_SWEEP):
        answers, game = random_answers(i), random_game(i)
        t, cap, gate, base_class = questionnaire_side(answers)
        level = None if game is None else game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
        want = expected_class(t, cap, gate, level)
        ctx = (i, t, cap, gate, level, want)

        r = post(client, "/profile", with_game(answers, game))
        assert r.status_code == 200, (ctx, r.text[:200])
        p = r.json()
        assert p["c_level"] == f"C{want}", (ctx, p["c_level"])
        assert number(p["c_level"]) <= cap or gate, ctx                             # never above the cap
        assert not gate or p["c_level"] == "C1", ctx                                # the gate forces C1
        assert p["max_r_level"] == number(p["c_level"]), ctx                        # eligibility follows the class
        if gate:
            assert p["binding_dimension"] == "gate", ctx
        elif game is not None and want != min(cap, t):
            assert p["binding_dimension"] == "game", ctx                            # the game moved the category
        else:
            assert p["binding_dimension"] == ("tolerance" if t <= cap else "capacity"), ctx
        if game is None:
            assert not p.get("game_played"), ctx                                    # absent / false unless a game was sent
            assert p["c_level"] == base_class, ctx
            seen["no_game"] += 1
        else:
            assert p["game_played"] is True and p["game_level"] == level, ctx
            assert p["questionnaire_class"] == base_class and p["blended_level"] == blended_level(t, level), ctx
            assert math.isclose(p["game_score"], game_score(game["tolerance"], game.get("loss_acceptance")), abs_tol=1e-6), ctx
        seen["classes"].add(p["c_level"])
        seen["gate"] += gate
        seen["lowered"] += number(p["c_level"]) < number(base_class)
        seen["raised"] += number(p["c_level"]) > number(base_class)
        seen["capped"] += (not gate) and game is not None and blended_level(t, level) > cap

        assert post(client, "/profile", with_game(answers, game)).json() == p, ctx           # deterministic

        form = post(client, "/preferences/form", {"answers": with_game(answers, game), "trading_platforms": []})
        assert form.status_code == 200, (ctx, form.text[:200])
        assert form.json()["c_level"] == p["c_level"] and form.json()["max_r_level"] == p["max_r_level"], ctx   # one class everywhere
        assert post(client, "/preferences/form", {"answers": with_game(answers, game), "trading_platforms": []}).json() == form.json(), ctx

    # the sweep must be a real one: several classes, the gate, a game that lowers, one that raises, one stopped by the cap
    assert len(seen["classes"]) >= 4 and seen["gate"] >= 1 and seen["lowered"] >= 5 and seen["raised"] >= 5, seen
    assert seen["capped"] >= 1 and seen["no_game"] >= 1, seen


@needs_blend
def test_may_raise_off_only_lowers_and_weight_zero_equals_no_game(client, monkeypatch):
    for i in range(min(N_SWEEP, 120)):
        answers, game = random_answers(i), random_game(i)
        if game is None:
            continue
        t, cap, gate, base_class = questionnaire_side(answers)
        level = game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
        monkeypatch.delenv(WEIGHT_ENV, raising=False)
        monkeypatch.setenv(RAISE_ENV, "off")
        p = post(client, "/profile", with_game(answers, game)).json()
        assert p["c_level"] == f"C{expected_class(t, cap, gate, level, may_raise=False)}", (i, t, cap, gate, level)
        assert number(p["c_level"]) <= number(base_class), i                        # a bold game cannot raise it
        monkeypatch.delenv(RAISE_ENV, raising=False)
        monkeypatch.setenv(WEIGHT_ENV, "0")
        zero = post(client, "/profile", with_game(answers, game)).json()
        monkeypatch.delenv(WEIGHT_ENV, raising=False)
        none = post(client, "/profile", answers).json()
        assert zero == none, i                                                      # weight 0: the game is ignored everywhere,
        for key in ("c_level", "max_r_level", "suitability_max_equity", "product_access", "blocked_product_types",
                    "tolerance_level", "capacity_level", "no_loss_gate", "binding_dimension"):
            assert zero[key] == none[key], (i, key)                                 # the profile does not even say a game was sent
        form_zero = None
        monkeypatch.setenv(WEIGHT_ENV, "0")
        form_zero = post(client, "/preferences/form", {"answers": with_game(answers, game), "trading_platforms": []}).json()
        monkeypatch.delenv(WEIGHT_ENV, raising=False)
        assert form_zero == post(client, "/preferences/form", {"answers": answers, "trading_platforms": []}).json(), i


@needs_blend
def test_the_products_on_offer_follow_the_blended_class(client):
    """/preferences/form and /preferences/areas: an option the BLENDED class does not allow is refused even if the
    questionnaire alone would allow it, and an option only the blended class allows is accepted."""
    checked = {"lowered": 0, "raised": 0}
    for i in range(N_SWEEP):
        answers, game = random_answers(i), random_game(i)
        if game is None:
            continue
        t, cap, gate, base_class = questionnaire_side(answers)
        blended = post(client, "/profile", with_game(answers, game)).json()["c_level"]
        if blended == base_class:
            continue
        for body_answers, klass in ((with_game(answers, game), blended), (answers, base_class)):
            form = post(client, "/preferences/form", {"answers": body_answers, "trading_platforms": []}).json()
            allowed = {o["id"] for q in form["questions"] for o in q["options"] if o["available"]}
            blocked = {o["id"] for q in form["questions"] for o in q["options"] if not o["available"]}
            if klass == blended:
                mine_allowed, mine_blocked = allowed, blocked
            else:
                other_allowed, other_blocked = allowed, blocked
        lower = number(blended) < number(base_class)
        checked["lowered" if lower else "raised"] += 1
        if lower:
            assert mine_allowed <= other_allowed, i                              # a lower class offers no more
            assert mine_blocked >= other_blocked, i
        else:
            assert mine_allowed >= other_allowed, i
        # /preferences/areas: choosing something only the other class allows is refused by the blended one (422), and not echoed
        only_other = sorted(other_allowed - mine_allowed) if lower else sorted(mine_allowed - other_allowed)
        if only_other:
            first = next(q for q in form["questions"] if any(o["id"] == only_other[0] for o in q["options"]))
            prefs = {"preferences_version": form["preferences_version"], "choices": {first["id"]: [only_other[0]]}}
            for q in form["questions"]:
                if q["required"] and q["id"] != first["id"]:
                    ok = [o["id"] for o in q["options"] if o["available"]]
                    prefs["choices"][q["id"]] = ok[:1]
            refused = post(client, "/preferences/areas", {"answers": with_game(answers, game), "trading_platforms": [], "preferences": prefs})
            accepted = post(client, "/preferences/areas", {"answers": answers, "trading_platforms": [], "preferences": prefs})
            if lower:
                assert refused.status_code == 422 and accepted.status_code in (200, 422), (i, refused.status_code, accepted.status_code)
            else:
                assert accepted.status_code == 422, (i, accepted.status_code)
            assert only_other[0] not in refused.text or lower is False
    assert checked["lowered"] >= 3 and checked["raised"] >= 3, checked


@needs_blend
def test_portfolio_and_trade_check_use_the_blended_class_for_products_and_limits(client):
    policies = CONFIG.c_levels
    report = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    built, checked = 0, 0
    for i in range(N_SWEEP):
        answers, game = random_answers(i), random_game(i)
        if game is None or built >= 14:
            continue
        t, cap, gate, base_class = questionnaire_side(answers)
        p = post(client, "/profile", with_game(answers, game)).json()
        if p["c_level"] == base_class:
            continue
        r = post(client, "/portfolio", portfolio_body(answers, game))
        assert r.status_code == 200, (i, r.text[:200])
        out = r.json()
        built += 1
        assert out["c_level"] == p["c_level"], i
        policy = policies[out["c_level"]]
        for inst in out["universe"]["instruments"]:
            assert inst["r_level"] <= p["max_r_level"], (i, inst["ticker"])                      # the products follow the class
        assert all(held["asset_id"] in {x["ticker"] for x in out["universe"]["instruments"]} for held in out["report"]["allocation"]), i
        assert out["report"]["equity_weight"] <= policy.max_equity + 1e-6, (i, policy.max_equity)    # and so do the limits
        assert out["report"]["crypto_weight"] <= policy.max_crypto + 1e-6, i
        assert out["report"]["risk_class"] == out["c_level"], i
        if checked < 4:
            c = post(client, "/trade-check", {**portfolio_body(answers, game), "ibkr_report": report,
                                              "planned": {"ticker": "VTI", "side": "buy", "amount": 1000}})
            assert c.status_code == 200, (i, c.text[:200])
            assert c.json()["check"]["investor_max_r_level"] == p["max_r_level"], i               # the check uses the same class
            checked += 1
    assert built >= 5 and checked >= 3, (built, checked)


# ====================================================================================== 3. hostile risk_game
def stamp(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


GOOD = {"tolerance": 55, "loss_acceptance": 40}
ECHO = "ZZ-SENTINEL-98765.4321"           # a value that must never come back inside a 422


def hostile_cases() -> list[tuple[str, object]]:
    ok = stamp(30)
    return [
        ("tolerance below range", {"tolerance": -1, "completed_at": ok}),
        ("tolerance above range", {"tolerance": 101, "completed_at": ok}),
        ("tolerance huge", {"tolerance": 1e308, "completed_at": ok}),
        ("tolerance float", {"tolerance": 55.5, "completed_at": ok}),
        ("tolerance string", {"tolerance": ECHO, "completed_at": ok}),
        ("tolerance list", {"tolerance": [55], "completed_at": ok}),
        ("tolerance object", {"tolerance": {"x": ECHO}, "completed_at": ok}),
        ("tolerance null", {"tolerance": None, "completed_at": ok}),
        ("tolerance bool", {"tolerance": True, "completed_at": ok}),
        ("tolerance missing", {"completed_at": ok}),
        ("loss_acceptance below", {**GOOD, "loss_acceptance": -5, "completed_at": ok}),
        ("loss_acceptance above", {**GOOD, "loss_acceptance": 1000, "completed_at": ok}),
        ("loss_acceptance string", {**GOOD, "loss_acceptance": ECHO, "completed_at": ok}),
        ("loss_acceptance float", {**GOOD, "loss_acceptance": 40.5, "completed_at": ok}),
        ("extra field", {**GOOD, "completed_at": ok, ECHO: 1}),
        ("extra nested field", {**GOOD, "completed_at": ok, "crash_test": {"max_drawdown_seen": 0.3, ECHO: 1}}),
        ("completed_at missing", dict(GOOD)),
        ("completed_at future", {**GOOD, "completed_at": stamp(-60 * 24)}),
        ("completed_at just future", {**GOOD, "completed_at": stamp(-60)}),
        ("completed_at before 2024", {**GOOD, "completed_at": "2023-12-31T23:59:00+00:00"}),
        ("completed_at year 1", {**GOOD, "completed_at": "0001-01-01T00:00:00+00:00"}),
        ("completed_at not a date", {**GOOD, "completed_at": ECHO}),
        ("completed_at number", {**GOOD, "completed_at": 98765.4321}),
        ("completed_at year 9999", {**GOOD, "completed_at": "9999-12-31T00:00:00+00:00"}),
        ("crash_test huge drawdown", {**GOOD, "completed_at": ok, "crash_test": {"max_drawdown_seen": 9.87654321e25}}),
        ("crash_test negative", {**GOOD, "completed_at": ok, "crash_test": {"max_drawdown_seen": -3.0}}),
        ("crash_test fraction above 1", {**GOOD, "completed_at": ok, "crash_test": {"max_drawdown_seen": 0.3, "sold_fraction": 7}}),
        ("crash_test string", {**GOOD, "completed_at": ok, "crash_test": ECHO}),
        ("risk_game is a number", 98765.4321),
        ("risk_game is a string", ECHO),
        ("risk_game is a list", [GOOD]),
        ("risk_game is empty", {}),
        ("risk_game is huge", {**GOOD, "completed_at": ok, "junk": "x" * 200_000}),
    ]


RAW_NUMBERS = [("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity")]


def assert_clean_refusal_or_acceptance(response, case):
    assert response.status_code < 500, (case, response.status_code, response.text[:200])      # never a 5xx
    if response.status_code >= 400:                                          # (a 200 /portfolio carries the report's html)
        assert "Traceback" not in response.text and "<html" not in response.text.lower(), case
    if response.status_code == 422:
        assert ECHO not in response.text and "9.87654321e+25" not in response.text and "98765.4321" not in response.text, case
        assert "ZZ-SENTINEL" not in response.text, case
        body = response.json()
        detail = body["detail"]
        if isinstance(detail, list):
            for error in detail:
                assert set(error) <= {"type", "loc", "msg"}, (case, error)                     # no `input`, no `ctx`


@needs_blend
@pytest.mark.parametrize("name, game", hostile_cases(), ids=[n for n, _ in hostile_cases()])
def test_hostile_risk_game_inside_the_answers(client, name, game):
    answers = random_answers(1)
    for path, body in (("/profile", {**answers, "risk_game": game}),
                       ("/preferences/form", {"answers": {**answers, "risk_game": game}, "trading_platforms": []}),
                       ("/portfolio", portfolio_body(answers) | {"answers": {**answers, "risk_game": game}})):
        response = post(client, path, body)
        assert_clean_refusal_or_acceptance(response, (name, path))
    if name in {"completed_at future", "completed_at just future", "completed_at before 2024", "completed_at year 1",
                "completed_at not a date", "completed_at number", "completed_at year 9999", "completed_at missing", "extra field",
                "extra nested field", "tolerance string", "tolerance above range", "tolerance below range", "loss_acceptance above",
                "tolerance null", "tolerance missing"}:        # (a bool may be read as 0/1 by pydantic's lax mode: not asserted)
        # the contract: a future (> 10 min), pre-2024 or malformed game is REFUSED with a plain 422 (never ignored, never accepted)
        assert post(client, "/profile", {**answers, "risk_game": game}).status_code == 422, name


@needs_blend
@pytest.mark.parametrize("age_minutes", [60, 25 * 60, 60 * 24 * 30, 60 * 24 * 365])
def test_an_old_game_result_is_still_valid(client, age_minutes):
    """No staleness rule: the browser keeps the result, and sealed contexts are re-checked hours later."""
    answers = random_answers(4)
    old = {"tolerance": 90, "loss_acceptance": 90, "completed_at": stamp(age_minutes)}
    new = {**old, "completed_at": stamp(1)}
    a, b = post(client, "/profile", {**answers, "risk_game": old}), post(client, "/profile", {**answers, "risk_game": new})
    assert a.status_code == 200 and b.status_code == 200, a.text[:200]
    assert a.json() == b.json()                                               # the age does not change the category


@needs_blend
def test_a_game_a_few_minutes_ahead_is_tolerated_but_not_an_hour_ahead(client):
    answers = random_answers(4)
    assert post(client, "/profile", {**answers, "risk_game": {**GOOD, "completed_at": stamp(-5)}}).status_code == 200   # clock skew
    assert post(client, "/profile", {**answers, "risk_game": {**GOOD, "completed_at": stamp(-30)}}).status_code == 422


@needs_blend
@pytest.mark.parametrize("token, raw", RAW_NUMBERS)
def test_non_finite_numbers_are_refused_not_crashed_on(client, token, raw):
    answers = random_answers(2)
    for field in ("tolerance", "loss_acceptance"):
        game = {"tolerance": 55, "loss_acceptance": 40, "completed_at": stamp(5)}
        text = json.dumps({**answers, "risk_game": {**game, field: "@@"}}).replace('"@@"', raw)
        response = client.post("/profile", content=text, headers={"Content-Type": "application/json"})
        assert_clean_refusal_or_acceptance(response, (field, token))
        assert response.status_code in (400, 422), (field, token, response.status_code)


@needs_blend
def test_a_game_in_two_places_is_refused_when_they_differ_and_fine_when_they_agree(client):
    answers = random_answers(11)
    t, cap, gate, base_class = questionnaire_side(answers)
    inside = {"tolerance": 5, "loss_acceptance": 5, "completed_at": stamp(10)}          # a very cautious game
    outside = {"tolerance": 100, "loss_acceptance": 100, "completed_at": stamp(10)}     # a very bold one, sent at the top level
    only_inside = post(client, "/portfolio", portfolio_body(answers, inside))
    only_outside = post(client, "/portfolio", {**portfolio_body(answers), "risk_game": outside})   # old clients: top level alone
    assert only_inside.status_code == 200 and only_outside.status_code == 200
    assert only_inside.json()["c_level"] == f"C{expected_class(t, cap, gate, game_level(5))}"
    assert only_outside.json()["c_level"] == f"C{expected_class(t, cap, gate, game_level(100))}"
    different = post(client, "/portfolio", {**portfolio_body(answers, inside), "risk_game": outside})
    assert different.status_code == 422, different.status_code                           # differing values: refused, never merged
    assert_clean_refusal_or_acceptance(different, "two games")
    assert '"input"' not in different.text and '"ctx"' not in different.text             # and nothing is echoed back
    agreeing = post(client, "/portfolio", {**portfolio_body(answers, inside), "risk_game": dict(inside)})
    assert agreeing.status_code == 200 and agreeing.json()["c_level"] == only_inside.json()["c_level"]   # equal values: fine
    # the same on the trade check
    report = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    planned = {"ticker": "VTI", "side": "buy", "amount": 1000}
    body = {**portfolio_body(answers, inside), "ibkr_report": report, "planned": planned}
    assert post(client, "/trade-check", {**body, "risk_game": outside}).status_code == 422
    assert post(client, "/trade-check", {**body, "risk_game": dict(inside)}).status_code == 200


# ====================================================================================== 4. sealed contexts
@needs_blend
def test_sealed_contexts_verify_and_carry_the_blended_class_and_a_replay_changes_only_new_builds(client):
    report = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    picked = None
    for i in range(N_SWEEP):
        answers = random_answers(i)
        t, cap, gate, base_class = questionnaire_side(answers)
        if gate or cap < 4 or t < 2 or t > 4:
            continue
        picked = (answers, t, cap, base_class)
        break
    assert picked, "no suitable investor in the sweep"
    answers, t, cap, base_class = picked
    cautious = {"tolerance": 5, "loss_acceptance": 5, "completed_at": stamp(20)}
    bold = {"tolerance": 98, "loss_acceptance": 98, "completed_at": stamp(10)}
    want_cautious, want_bold = f"C{expected_class(t, cap, False, 1)}", f"C{expected_class(t, cap, False, 5)}"
    assert want_cautious != want_bold

    first = post(client, "/portfolio", portfolio_body(answers, cautious)).json()
    assert first["c_level"] == want_cautious
    chat = open_context(first["chat_context"])                                          # verifies the seal
    assert chat["c_level"] == want_cautious
    what_if = open_context(first["what_if_context"])                                    # sealed too (never shown to the LLM)
    assert what_if["c_level"] == want_cautious and what_if["max_r_level"] == int(want_cautious[1:])
    assert all(level <= what_if["max_r_level"] for level in what_if["r_levels"].values())

    trade = post(client, "/trade-check", {**portfolio_body(answers, cautious), "ibkr_report": report,
                                           "planned": {"ticker": "VTI", "side": "buy", "amount": 1000}}).json()
    facts = open_context(trade["trade_context"])
    assert facts["investor_max_r_level"] == int(want_cautious[1:])

    # the game is played again later: a NEW build changes class, the sealed contexts of the old build do not
    second = post(client, "/portfolio", portfolio_body(answers, bold)).json()
    assert second["c_level"] == want_bold
    assert open_context(second["chat_context"])["c_level"] == want_bold
    assert open_context(first["chat_context"])["c_level"] == want_cautious              # the old one still says what it said
    assert first["chat_context"] != second["chat_context"]
    assert post(client, "/portfolio", portfolio_body(answers, cautious)).json()["c_level"] == want_cautious   # replaying the first game: the first class

    # a tampered seal does not verify
    token = first["chat_context"]
    broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(ContextError):
        open_context(broken)


@needs_blend
@pytest.mark.parametrize("raw, weight", [("0.1", 0.1), ("0.25", 0.25), ("0.5", 0.5), ("0.9", 0.5), ("7", 0.5), ("-1", 0.0), ("-0.0", 0.0),
                                         ("abc", 1 / 3), ("", 1 / 3), ("nan", 1 / 3), ("inf", 1 / 3), ("-inf", 1 / 3),
                                         ("1e999", 1 / 3), (" 0.2 ", 0.2)])
def test_every_weight_setting_gives_the_class_of_its_clamped_value_and_never_a_5xx(client, monkeypatch, raw, weight):
    """EGOQUANT_RISK_GAME_WEIGHT is clamped to 0..0.5; junk means the default one third."""
    monkeypatch.setenv(WEIGHT_ENV, raw)
    for i in range(0, 60, 2):
        answers, game = random_answers(i), random_game(i + 1) or {"tolerance": 90, "completed_at": stamp(5)}
        t, cap, gate, _ = questionnaire_side(answers)
        level = game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
        r = post(client, "/profile", with_game(answers, game))
        assert r.status_code == 200, (raw, r.status_code, r.text[:150])
        assert r.json()["c_level"] == f"C{expected_class(t, cap, gate, level, weight=weight)}", (raw, i, t, cap, gate, level)


@needs_blend
@pytest.mark.parametrize("raw, may_raise", [("off", False), ("OFF", False), ("0", False), ("false", False), ("no", False),
                                            ("on", True), ("", True), ("1", True), ("yes", True), ("garbage", True)])
def test_every_may_raise_setting(client, monkeypatch, raw, may_raise):
    monkeypatch.setenv(RAISE_ENV, raw)
    for i in range(0, 60, 2):
        answers, game = random_answers(i), random_game(i + 1) or {"tolerance": 95, "completed_at": stamp(5)}
        t, cap, gate, _ = questionnaire_side(answers)
        level = game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
        r = post(client, "/profile", with_game(answers, game))
        assert r.status_code == 200 and r.json()["c_level"] == f"C{expected_class(t, cap, gate, level, may_raise=may_raise)}", (raw, i)
