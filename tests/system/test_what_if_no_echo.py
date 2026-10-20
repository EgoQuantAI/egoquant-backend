"""/what-if and /privacy-log never send back what was posted: hostile scenario values, forged contexts, odd ids.

Reuses the fixtures of test_what_if.py (one real synthetic /portfolio build) and the FakeStore of test_accounts.py.
"""

import pytest
from fastapi.testclient import TestClient

from egoquant.accounts.store import set_store
from tests.accounts.test_accounts import A, FakeStore
from tests.system.test_what_if import bound, built, env_and_limits  # noqa: F401  (fixtures)

SECRET = "FAKESECRETTOKEN0123456789"


@pytest.fixture
def api():
    from main import app
    set_store(FakeStore())
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        set_store(None)


def calm(response):
    assert response.status_code < 500, (response.status_code, response.text[:300])
    assert SECRET not in response.text
    return response


@pytest.mark.parametrize("scenario", [
    {"horizon_years": SECRET}, {"extra_monthly_contribution": SECRET}, {SECRET: 1}, {"risk_class": SECRET},
    {"horizon_years": [SECRET]}, {"horizon_years": {"x": SECRET}}, {"note": SECRET}, {"horizon_years": None},
    {"extra_monthly_contribution": 1e308}, {"horizon_years": -5}], ids=lambda s: str(list(s)[0])[:12])
def test_hostile_scenarios_get_a_plain_answer_without_echo(api, built, scenario):  # noqa: F811
    token = bound(built["token"], "user-a")
    out = calm(api.post("/what-if", json={"what_if_context": token, "scenario": scenario}, headers=A))
    assert out.status_code in (200, 422)


@pytest.mark.parametrize("context", [SECRET, SECRET * 100, "a.b", "a.b.c.d", "\x00", "𝔘𝔫𝔦-日本語"],
                         ids=["secret", "long", "two-parts", "four-parts", "nul", "unicode"])
def test_forged_contexts_are_refused_without_echo(api, context):
    out = calm(api.post("/what-if", json={"what_if_context": context, "scenario": {}}, headers=A))
    assert out.status_code in (403, 422)


def test_unknown_and_missing_fields_are_422_without_echo(api, built):  # noqa: F811
    token = bound(built["token"], "user-a")
    for body in ({"what_if_context": token, "scenario": {}, SECRET: 1}, {"scenario": {}}, {"what_if_context": token},
                 {"what_if_context": token, "scenario": SECRET}, {"what_if_context": 5, "scenario": {}}):
        assert calm(api.post("/what-if", json=body, headers=A)).status_code == 422


@pytest.mark.parametrize("session_id", ["short", "x" * 65, SECRET + "!", "a b c d e f g h", "..%2F..%2Fetc", "𝔘𝔫𝔦𝔠𝔬𝔡𝔢𝔘𝔫𝔦𝔠𝔬𝔡𝔢"],
                         ids=["short", "long", "bang", "space", "path", "unicode"])
def test_privacy_log_refuses_odd_session_ids(api, session_id):
    out = calm(api.get(f"/privacy-log/{session_id}", headers=A))
    assert out.status_code in (404, 422)


def test_privacy_log_needs_sign_in(api):
    assert calm(api.get("/privacy-log/session-12345")).status_code == 401
