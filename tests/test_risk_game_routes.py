"""The Risk Game inside the risk category, through the HTTP routes (docs/RISK_GAME_BLEND.md). Synthetic prices: no network.

The category is the LOWEST of the capacity cap and the blended tolerance (two thirds questionnaire, one third game); no game sent
means exactly today's behaviour; one game result rides with the answers so every endpoint scores the same category.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant import risk_profile, settings
from egoquant.demo.personas import PERSONAS
from tests.test_egoquant_routes import answers_body, portfolio_body

pytestmark = pytest.mark.skipif(not hasattr(risk_profile, "GameInput"), reason="the risk profile has no Risk Game yet")

NOW = datetime.now(timezone.utc)


def game(tolerance: int, loss: int | None = None, **extra) -> dict:
    body = {"tolerance": tolerance, "completed_at": (NOW - timedelta(minutes=5)).isoformat(), **extra}
    if loss is not None:
        body["loss_acceptance"] = loss
    return body


def with_game(body: dict, **kw) -> dict:
    return {**body, "risk_game": kw}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "off")
    for name in ("EGOQUANT_RISK_GAME_WEIGHT", "EGOQUANT_RISK_GAME_MAY_RAISE"):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    app.include_router(egoquant_routes.router)
    egoquant_routes._portfolio_timestamps.clear()
    egoquant_routes._debug_timestamps.clear()
    return TestClient(app)


def persona_answers(key: str = "balanced_saver") -> dict:
    return answers_body(PERSONAS[key])


def game_level(tolerance: int, loss: int | None) -> int:
    score = tolerance if loss is None else (tolerance + loss) / 2
    return min(5, 1 + int(score // 20))


def expected_class(t: int, cap: int, g: int | None) -> int:
    blend = t if g is None else (2 * t + g) / 3
    return max(1, min(cap, round(blend)))


# ---- /profile -------------------------------------------------------------------------------------------------------------
def test_no_game_is_exactly_todays_answer(client):
    data = client.post("/profile", json=persona_answers()).json()
    assert not {"game_played", "game_score", "game_level", "blended_level", "questionnaire_class"} & set(data)


@pytest.mark.parametrize("tolerance,loss", [(5, 5), (30, 20), (50, None), (75, 65), (100, 100), (0, 0)])
def test_the_category_is_the_lowest_of_the_cap_and_the_blend(client, tolerance, loss):
    plain = client.post("/profile", json=persona_answers()).json()
    t, cap = plain["tolerance_level"], plain["capacity_level"]
    data = client.post("/profile", json={**persona_answers(), "risk_game": game(tolerance, loss)}).json()
    g = game_level(tolerance, loss)
    assert data["game_played"] is True and data["game_level"] == g
    assert data["game_score"] == pytest.approx(tolerance if loss is None else (tolerance + loss) / 2)
    assert data["questionnaire_class"] == plain["c_level"]
    if plain["no_loss_gate"]:
        assert data["c_level"] == "C1"
    else:
        assert data["blended_level"] == expected_class(t, 5, g)
        assert data["c_level"] == f"C{expected_class(t, cap, g)}"
        assert int(data["c_level"][1]) <= cap


def test_a_cautious_game_lowers_and_a_bold_one_never_passes_the_cap(client):
    answers = persona_answers("cautious_retiree")                 # a low capacity persona
    plain = client.post("/profile", json=answers).json()
    bold = client.post("/profile", json={**answers, "risk_game": game(100, 100)}).json()
    assert int(bold["c_level"][1]) <= plain["capacity_level"]
    careful = client.post("/profile", json={**persona_answers("growth_investor"), "risk_game": game(0, 0)}).json()
    base = client.post("/profile", json=persona_answers("growth_investor")).json()
    assert int(careful["c_level"][1]) <= int(base["c_level"][1])


def test_the_weight_zero_kill_switch_means_no_game(client, monkeypatch):
    monkeypatch.setenv("EGOQUANT_RISK_GAME_WEIGHT", "0")
    plain = client.post("/profile", json=persona_answers()).json()
    data = client.post("/profile", json={**persona_answers(), "risk_game": game(100, 100)}).json()
    assert data["c_level"] == plain["c_level"] and not data.get("game_played")


def test_the_game_can_be_told_to_only_lower(client, monkeypatch):
    plain = client.post("/profile", json=persona_answers("growth_investor")).json()
    monkeypatch.setenv("EGOQUANT_RISK_GAME_MAY_RAISE", "off")
    data = client.post("/profile", json={**persona_answers("growth_investor"), "risk_game": game(100, 100)}).json()
    assert int(data["c_level"][1]) <= int(plain["c_level"][1])


# ---- one result, everywhere -----------------------------------------------------------------------------------------------
def test_portfolio_profile_and_report_agree_on_the_category(client):
    answers = {**persona_answers(), "risk_game": game(20, 10)}
    profile = client.post("/profile", json=answers).json()
    body = portfolio_body(PERSONAS["balanced_saver"])
    body["answers"] = answers
    out = client.post("/portfolio", json=body).json()
    assert out["c_level"] == profile["c_level"]
    assert out["report"]["category_basis"]["final"] == profile["c_level"]
    assert "one third" in out["game_note"] and profile["c_level"] in out["game_note"]
    assert any(r["kind"] == "class" and "Risk Game" in r["text"] for r in out["report"]["reasons"])


def test_a_portfolio_without_a_game_has_no_category_basis(client):
    out = client.post("/portfolio", json=portfolio_body(PERSONAS["balanced_saver"])).json()
    assert "category_basis" not in out["report"]
    assert out["game_note"].startswith("Risk Game: not played")


def test_the_old_top_level_risk_game_still_works_and_the_two_must_agree(client):
    base = portfolio_body(PERSONAS["balanced_saver"])
    one = client.post("/portfolio", json={**base, "risk_game": game(60, 60)})
    assert one.status_code == 200
    answers = {**base["answers"], "risk_game": game(60, 60)}
    both = client.post("/portfolio", json={**base, "answers": answers, "risk_game": answers["risk_game"]})
    assert both.status_code == 200
    clash = client.post("/portfolio", json={**base, "answers": answers, "risk_game": game(10, 10)})
    assert clash.status_code == 422 and "once" in clash.text


def test_preferences_follow_the_blended_category(client):
    persona = PERSONAS["balanced_saver"]
    base = {"answers": answers_body(persona), "trading_platforms": list(persona.trading_platforms)}
    plain = client.post("/preferences/form", json=base).json()
    careful = client.post("/preferences/form", json={**base, "answers": {**base["answers"], "risk_game": game(0, 0)}}).json()
    unavailable = lambda form: sum(1 for q in form["questions"] for o in q["options"] if not o.get("available", True))
    assert unavailable(careful) >= unavailable(plain)


def test_agent_b_does_not_tighten_on_the_game_a_second_time(client):
    persona = PERSONAS["growth_investor"]
    body = portfolio_body(persona, goal_amount=round(persona.amount_invested * 1.5, -2), monthly_flow=0.0)
    body["answers"] = {**body["answers"], "risk_game": game(0, 0)}
    _, _, run, note, *_ = egoquant_routes._execute(egoquant_routes.PortfolioIn(**body))
    assert run.profile.game_played is True
    assert "game_part_of_category" in run.screening.report.flags                  # Agent B says it did not count it again
    assert not any("game" in str(getattr(c, "source", "")) for c in run.result.behavioral_constraints)
    assert "can only make the portfolio more careful" not in (note or "").split("Crash test")[0]
    # and a build without the game (or with the weight at 0, the kill switch) leaves Agent B's game handling as it was
    body["answers"] = {**body["answers"], "risk_game": None}
    _, _, plain, *_ = egoquant_routes._execute(egoquant_routes.PortfolioIn(**body))
    assert "game_part_of_category" not in plain.screening.report.flags


# ---- validation -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    game(101), game(-1), game(50, 101), game(50, -5), game(float("nan")), game(float("inf")), game("high"),
    {**game(50), "extra": 1}, game(50, crash_test={"max_drawdown_seen": 2}),
    {"tolerance": 50, "completed_at": (NOW + timedelta(hours=2)).isoformat()},
    {"tolerance": 50, "completed_at": "2020-01-01T00:00:00+00:00"},
    {"tolerance": 50, "completed_at": "yesterday"},
])
def test_a_bad_game_result_is_a_plain_422(client, bad, monkeypatch):
    import main                                          # the real app: its 422 handler never repeats what the client sent
    raw = json.dumps({**persona_answers(), "risk_game": bad}, allow_nan=True)        # NaN must reach the server as text
    r = TestClient(main.app).post("/profile", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert "nan" not in r.text.lower() and "high" not in r.text.lower()


def test_an_old_game_result_is_fine(client):
    old = {"tolerance": 50, "completed_at": "2025-03-04T10:00:00+00:00"}
    assert client.post("/profile", json={**persona_answers(), "risk_game": old}).status_code == 200


# ---- settings -------------------------------------------------------------------------------------------------------------
def test_the_environment_settings(monkeypatch):
    for name in ("EGOQUANT_RISK_GAME_WEIGHT", "EGOQUANT_RISK_GAME_MAY_RAISE"):
        monkeypatch.delenv(name, raising=False)
    assert settings.risk_game_weight() is None and settings.risk_game_may_raise() is None and settings.risk_game_options() == {}
    for raw, want in (("0", 0.0), ("0.2", 0.2), ("0.9", 0.5), ("-1", 0.0), (" 0.25 ", 0.25), ("abc", None), ("", None), ("nan", None), ("inf", None)):
        monkeypatch.setenv("EGOQUANT_RISK_GAME_WEIGHT", raw)
        assert settings.risk_game_weight() == want, raw
    monkeypatch.setenv("EGOQUANT_RISK_GAME_MAY_RAISE", "Off")
    assert settings.risk_game_may_raise() is False
    monkeypatch.setenv("EGOQUANT_RISK_GAME_MAY_RAISE", "on")
    assert settings.risk_game_may_raise() is None


# ---- the other places that score the answers: chat facts and the what-if re-derivation ------------------------------------
def test_chat_facts_and_the_what_if_agent_use_the_same_category(client, monkeypatch):
    from chat import _ground, ChatRequest
    from egoquant.agents import what_if
    answers = {**persona_answers(), "risk_game": game(10, 10)}
    profile = client.post("/profile", json=answers).json()
    facts, _, _ = _ground(ChatRequest(messages=[{"role": "user", "content": "hello"}], answers=answers))
    assert facts["c_level"] == profile["c_level"]
    assert facts["risk_game"]["game_level"] == profile["game_level"] and facts["risk_game"]["played"] is True
    plain_facts, _, _ = _ground(ChatRequest(messages=[{"role": "user", "content": "hello"}], answers=persona_answers()))
    assert "risk_game" not in plain_facts
    agent = what_if._market_agent({"inputs": {"answers": answers, "trading_platforms": []}, "interest_areas": ["stocks"],
                                   "data_source": "synthetic"}, ["VT"])
    assert agent.interests.max_r_level == profile["max_r_level"]
