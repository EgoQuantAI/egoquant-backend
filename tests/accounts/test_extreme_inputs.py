"""Extreme and hostile input on /account/*, /trade-check and /account/trade-check, through the real app (main.app).

Whatever is sent: never a 5xx, never an echo of what was sent, never another user's data. Supabase is the FakeStore
of test_accounts.py. Invented data only.
"""

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from egoquant.accounts import routes as account_routes
from egoquant.accounts.routes import CONSENT_VERSION
from tests.accounts.test_accounts import A, B, DATA, portfolio_request, store  # noqa: F401  (store is a fixture)

SECRET = "FAKESECRETTOKEN0123456789"
CHECK_ID = "00000000-0000-4000-8000-000000000009"
UNICODE = "𝔘𝔫𝔦𝔠𝔬𝔡𝔢-ñ-日本語-‮-\x00-\U0001f600"


@pytest.fixture
def api(store):  # noqa: F811
    from main import app
    import egoquant_routes
    egoquant_routes._portfolio_timestamps.clear()
    return TestClient(app, raise_server_exceptions=False)       # a crash must show up as a 500, not an exception


def post(api, path, body=None, headers=A, raw=None):
    if raw is not None:
        return api.post(path, content=raw, headers={**headers, "Content-Type": "application/json"})
    return api.post(path, json=body, headers=headers)


def calm(response, *, forbidden=(SECRET,)):
    assert response.status_code < 500, (response.status_code, response.text[:300])
    for text in forbidden:
        assert text not in response.text
    return response


# ---------------------------------------------------------------- consent and import
@pytest.mark.parametrize("body", [
    None, {}, {"scope": None, "version": None}, {"scope": SECRET, "version": SECRET}, {"scope": ["ibkr_data"]},
    {"scope": "ibkr_data", "version": SECRET * 40}, {"scope": "ibkr_data", "version": UNICODE},
    {"scope": "ibkr_data", "version": CONSENT_VERSION, "user_id": SECRET}, [SECRET], SECRET, 12345])
def test_consent_refuses_garbage_without_echo(api, body):
    out = calm(post(api, "/account/consent", body))
    assert out.status_code in (409, 422)


@pytest.mark.parametrize("body", [
    {}, {"ibkr_report": None, "ibkr_flex": None}, {"ibkr_report": SECRET}, {"ibkr_report": "<x/>" * 10},
    {"ibkr_report": UNICODE}, {"ibkr_report": "x" * 2_000_001}, {"ibkr_report": 5}, {"ibkr_report": ["a"]},
    {"ibkr_report": "a,b\n1,2", "ibkr_flex": {"token": SECRET, "query_id": "1"}},
    {"ibkr_flex": {"token": SECRET, "query_id": "x"}}, {"ibkr_flex": {"token": UNICODE, "query_id": "1"}},
    {"ibkr_flex": {"token": SECRET * 50, "query_id": "1"}}, {"ibkr_flex": None, "ibkr_filename": "../../etc/passwd"},
    {"ibkr_report": "a", "ibkr_filename": "x" * 300}])
def test_import_refuses_garbage_without_echo(api, body):
    post(api, "/account/consent", {"scope": "ibkr_data", "version": CONSENT_VERSION})
    out = calm(post(api, "/account/trades/import", body))
    assert out.status_code in (403, 422, 429)


def test_import_survives_a_formula_laden_report_and_stores_nothing(api, store):
    post(api, "/account/consent", {"scope": "ibkr_data", "version": CONSENT_VERSION})
    evil = (DATA / "ibkr_cautious_seller.xml").read_text(encoding="utf-8").replace("VTI", "=cmd|' /C calc'!A0", 1)
    out = calm(post(api, "/account/trades/import", {"ibkr_report": evil}), forbidden=("calc",))
    assert out.status_code in (200, 422)
    assert not any("=cmd" in json.dumps(rows) for rows in store.tables.values())


# ---------------------------------------------------------------- headers, tokens, ids
@pytest.mark.parametrize("header", [{}, {"Authorization": ""}, {"Authorization": "Bearer "}, {"Authorization": "Bearer " + SECRET},
                                    {"Authorization": "Bearer " + "A" * 100_000}, {"Authorization": "Basic abc"},
                                    {"Authorization": "Bearer token-a, Bearer token-b"}])
def test_bad_tokens_are_401_and_never_echoed(api, header):
    out = calm(api.get("/account/me", headers=header))
    assert out.status_code == 401


@pytest.mark.parametrize("path", ["/account/reports/" + quote(UNICODE), "/account/reports/" + "0" * 5000,
                                  "/account/reports/..%2F..%2Fetc", "/account/trade-checks/" + SECRET + "/decision"])
def test_bad_ids_are_refused(api, path):
    for method in (api.get, api.delete):
        out = calm(method(path, headers=A))
        assert out.status_code in (404, 405, 422)


@pytest.mark.parametrize("name", ["a", "A" * 21, "bad name", UNICODE, "x'; drop table profiles;--", "%00", ""])
def test_username_check_refuses_odd_names(api, name):
    out = calm(api.get("/account/username-available", params={"name": name}))
    assert out.status_code == 422


# ---------------------------------------------------------------- the decision on a trade check
def saved_check(store, user_id="user-a"):  # noqa: F811
    store.tables.setdefault("trade_checks", []).append(
        {"id": CHECK_ID, "user_id": user_id, "verdict": "advisable",
         "planned": {"ticker": "VTI", "side": "buy", "amount": 1500}, "result": {}, "user_decision": None})
    return f"/account/trade-checks/{CHECK_ID}/decision"


@pytest.mark.parametrize("body", [
    None, {}, {"decision": None}, {"decision": SECRET}, {"decision": "made", "quantity": SECRET, "price": 1},
    {"decision": "made", "quantity": None, "price": None}, {"decision": "made", "quantity": 1e308, "price": 1e308},
    {"decision": "made", "quantity": 1, "price": 1, "trade_date": SECRET},
    {"decision": "made", "quantity": 1, "price": 1, "trade_date": "0000-00-00"},
    {"decision": "made", "quantity": 1, "price": 1, "trade_date": "9999-12-31"},
    {"decision": "made", "quantity": 1, "price": 1, "currency": UNICODE},
    {"decision": "made", "quantity": [1], "price": {"x": 1}}, {"decision": "made", "quantity": True, "price": True}])
def test_decision_refuses_garbage_without_echo_and_records_no_trade(api, store, body):  # noqa: F811
    path = saved_check(store)
    out = calm(post(api, path, body))
    assert out.status_code in (422, 200)                       # True/True parses as 1/1: a valid, tiny trade
    if out.status_code == 422:
        assert store.tables.get("trades", []) == []


def test_raw_nan_and_infinity_in_json_are_refused(api, store):  # noqa: F811
    path = saved_check(store)
    for raw in ('{"decision":"made","quantity":NaN,"price":1}', '{"decision":"made","quantity":1,"price":Infinity}',
                '{"decision":"made","quantity":-Infinity,"price":1}'):
        assert calm(post(api, path, raw=raw)).status_code == 422
    assert store.tables.get("trades", []) == []


# ---------------------------------------------------------------- the trade check itself
def visitor(report=None, **kw):
    sent = {"ibkr_report": report if report is not None else (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")}
    return {**portfolio_request(**sent), **kw}


@pytest.mark.parametrize("planned", [
    None, {}, {"ticker": None, "side": "buy", "amount": 1}, {"ticker": SECRET * 5, "side": "buy", "amount": 1},
    {"ticker": UNICODE, "side": "buy", "amount": 1}, {"ticker": "VTI", "side": "BUY", "amount": 1},
    {"ticker": "VTI", "side": "buy", "amount": 0}, {"ticker": "VTI", "side": "buy", "amount": 1e13},
    {"ticker": "VTI", "side": "buy", "amount": [1]}, {"ticker": "VTI", "side": "buy", "amount": "lots"},
    {"ticker": "VTI", "side": "buy", "amount": 1, "ignore_limits": True}])
def test_visitor_trade_check_refuses_bad_trades_without_echo(api, planned):
    out = calm(post(api, "/trade-check", visitor(planned=planned), headers={}))
    assert out.status_code == 422


def test_visitor_trade_check_with_too_many_holdings_or_no_history_is_refused(api):
    many = {f"T{i}": 0.01 for i in range(100)}
    for body in (visitor(current_holdings=many, planned={"ticker": "VTI", "side": "buy", "amount": 1}),
                 {**portfolio_request(), "planned": {"ticker": "VTI", "side": "buy", "amount": 1}}):
        assert calm(post(api, "/trade-check", body, headers={})).status_code == 422


@pytest.mark.parametrize("report", ["", " ", UNICODE, "\x00" * 100, "<!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>",
                                    "a,b,c\n" * 50_000, "{" * 10_000],
                         ids=["empty", "space", "unicode", "nulls", "entity", "many-rows", "braces"])
def test_visitor_trade_check_survives_hostile_reports(api, report):
    body = visitor(report=report, planned={"ticker": "VTI", "side": "buy", "amount": 100})
    out = calm(post(api, "/trade-check", body, headers={}), forbidden=("ENTITY",))
    assert out.status_code == 422


def test_trade_check_flex_token_never_comes_back(api):
    body = {**portfolio_request(), "ibkr_flex": {"token": SECRET, "query_id": "123"},
            "planned": {"ticker": "VTI", "side": "buy", "amount": 100}}
    out = calm(post(api, "/trade-check", body, headers={}))
    assert SECRET not in out.text


def test_one_users_rate_limit_does_not_lock_out_another(api):
    for _ in range(account_routes._USER_LIMIT):
        assert api.get("/account/me", headers=A).status_code == 200
    assert api.get("/account/me", headers=A).status_code == 429
    assert api.get("/account/me", headers=B).status_code == 200


def test_the_trade_check_shares_the_portfolio_rate_limit(api):
    import egoquant_routes
    body = visitor(planned={"ticker": "VTI", "side": "buy", "amount": 100})
    seen = set()
    for _ in range(egoquant_routes._PORTFOLIO_LIMIT + 3):
        seen.add(post(api, "/trade-check", {**body, "ibkr_report": "x"}, headers={}).status_code)
    assert 429 in seen and all(code < 500 for code in seen)
