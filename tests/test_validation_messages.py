"""A 422 names a field in words and says the rule, never the value the client sent (main.py, Bartu's T11)."""

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.test_egoquant_routes import portfolio_body
from egoquant.demo.personas import PERSONAS

client = TestClient(app)
PERSONA = next(p for p in PERSONAS.values() if p.expected_class == "C3")


def first_error(response):
    assert response.status_code == 422, response.text
    return response.json()["detail"][0]


@pytest.mark.parametrize("change,label,msg", [
    ({"horizon_years": 0}, "Horizon in years", "must be above 0"),
    ({"min_cash_buffer": 2}, "Cash buffer", "must be 1 or less"),
    ({"liquidity_need": -1}, "Cash you may need soon", "must be 0 or more"),
    ({"goal_amount": -5}, "Goal amount", "must be above 0"),
    ({"horizon_years": "soon"}, "Horizon in years", "must be a number"),
])
def test_portfolio_numbers_are_named_in_words(change, label, msg):
    error = first_error(client.post("/portfolio", json=portfolio_body(PERSONA, **change)))
    assert error["loc"] == ["body", label] and error["msg"] == msg
    assert set(error) == {"type", "loc", "msg"}


def test_a_missing_field_and_a_game_score_are_named_too():
    body = portfolio_body(PERSONA)
    del body["horizon_years"]
    assert first_error(client.post("/portfolio", json=body))["msg"] == "is required"
    answers = {**portfolio_body(PERSONA)["answers"], "risk_game": {"tolerance": 101, "completed_at": "2026-10-19T10:00:00+00:00"}}
    error = first_error(client.post("/profile", json=answers))
    assert error["loc"] == ["body", "Risk Game score"] and error["msg"] == "must be 100 or less"


def test_unknown_fields_and_our_own_value_errors_keep_their_text_without_the_python_prefix():
    error = first_error(client.post("/portfolio", json=portfolio_body(PERSONA, trading_platforms=["my_bank"])))
    assert not error["msg"].startswith("Value error")
    extra = first_error(client.post("/portfolio", json=portfolio_body(PERSONA, secret_field_xyz=1)))
    assert extra["loc"][-1] == "(field not allowed)" and "secret_field_xyz" not in str(extra)


def test_the_value_sent_is_never_in_the_answer():
    r = client.post("/portfolio", json=portfolio_body(PERSONA, horizon_years="SENTINEL-VALUE-123"))
    assert r.status_code == 422 and "SENTINEL-VALUE-123" not in r.text
