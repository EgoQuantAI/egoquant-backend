"""POST /preferences/areas: the interest areas a portfolio request would use, before the request is made."""

import pytest
from fastapi.testclient import TestClient

from egoquant.demo.personas import PERSONAS
from main import app
from tests.test_egoquant_routes import portfolio_body

client = TestClient(app, raise_server_exceptions=False)


def body(persona="growth_investor", **choices):
    full = portfolio_body(PERSONAS[persona])
    prefs = PERSONAS[persona].preference_answers()
    chosen = {**prefs.choices, **choices}
    return {"answers": full["answers"], "trading_platforms": full.get("trading_platforms", []),
            "preferences": {"preferences_version": prefs.preferences_version, "choices": chosen}}


def test_areas_are_listed_in_the_order_the_build_uses_with_the_limit():
    out = client.post("/preferences/areas", json=body())
    assert out.status_code == 200
    data = out.json()
    assert data["limit"] == 8 and isinstance(data["areas"], list) and data["areas"]
    assert len(data["areas"]) == len(set(data["areas"]))


def test_the_areas_match_what_the_portfolio_request_is_built_from():
    from egoquant.preferences import build_form, load_preference_bank, to_interest_fields
    from egoquant.risk_profile import build_risk_profile, load_all
    import egoquant_routes
    request = body()
    form = build_form(egoquant_routes.PREF_BANK, egoquant_routes._score(egoquant_routes.AnswersIn(**request["answers"]).to_answers()),
                      egoquant_routes.PROFILE_CONFIG, request["trading_platforms"])
    expected = to_interest_fields(egoquant_routes.PreferenceAnswersIn(**request["preferences"]).to_answers(), form,
                                  egoquant_routes.PREF_BANK)["interest_areas"]
    assert client.post("/preferences/areas", json=request).json()["areas"] == expected


@pytest.mark.parametrize("bad", [{"P1": ["not-an-option"]}, {"P9": ["stocks"]}])
def test_an_option_that_is_not_offered_is_a_422_without_echo(bad):
    out = client.post("/preferences/areas", json=body(**bad))
    assert out.status_code in (409, 422) and "not-an-option" not in out.text


def test_unknown_fields_and_a_changed_question_set_are_refused():
    request = body()
    assert client.post("/preferences/areas", json={**request, "extra": 1}).status_code == 422
    request["preferences"]["preferences_version"] = "old-version"
    assert client.post("/preferences/areas", json=request).status_code == 409


# ---- the sentence a person reads when two answers clash (QA, 20 Oct): the form's own words, never a question code or a raw id ----
def portfolio_request_with(prefs_body):
    full = portfolio_body(PERSONAS["growth_investor"])
    full.pop("interest_areas", None)
    return {**full, "preferences": prefs_body["preferences"]}


def test_an_industry_that_is_wanted_and_avoided_is_said_in_plain_words_on_both_routes():
    import egoquant_routes
    request = body("growth_investor", P1=["stocks"], P2=["technology"], P5=["technology"])
    egoquant_routes._portfolio_timestamps.clear()
    for path, payload in (("/preferences/areas", request), ("/portfolio", portfolio_request_with(request))):
        out = client.post(path, json=payload)
        assert out.status_code == 422, (path, out.text)
        detail = out.json()["detail"]
        assert "Technology" in detail and "interested in" in detail and "avoid" in detail, (path, detail)
        assert not any(code in detail for code in ("P2", "P5", "[", "'technology'")), (path, detail)


def test_an_unknown_option_or_question_gets_one_generic_sentence_and_nothing_is_echoed():
    import egoquant_routes
    for bad in ({"P1": ["stocks", "canary-option-77"]}, {"P99": ["canary-option-77"]}):
        request = body("growth_investor", **bad)
        egoquant_routes._portfolio_timestamps.clear()
        for path, payload in (("/preferences/areas", request), ("/portfolio", portfolio_request_with(request))):
            out = client.post(path, json=payload)
            assert out.status_code == 422, (path, bad, out.text)
            assert "canary-option-77" not in out.text and "P99" not in out.text, (path, out.text)
            assert "not on the investment form" in out.json()["detail"], (path, out.text)
