"""/account/trade-check and "I made this trade": consent, saved checks, access control, the recorded trade.

check_trade itself is tested in tests/system/test_trade_check.py; here it is replaced by a stub that records
what the route handed it. Supabase is the FakeStore of test_accounts.py (same Row-Level Security rule).
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from egoquant.accounts.routes import CONSENT_VERSION
from egoquant.trade_check import TradeCheckError
from tests.accounts.test_accounts import A, B, app_client, client, consent, portfolio_request, store, upload  # noqa: F401

PLANNED = {"ticker": "vti", "side": "buy", "amount": 1500}
CHECK_ID = "00000000-0000-4000-8000-000000000001"


class StubCheck:
    def __init__(self, verdict, planned):
        self.verdict = verdict
        self._planned = planned

    def model_dump(self, mode="python"):
        return {"verdict": self.verdict, "planned": self._planned.model_dump(), "reasons": [], "label": "stub"}


class Stub:
    """Stands in for check_trade: remembers its arguments, answers `verdict` (or raises `error`)."""

    def __init__(self, verdict="advisable", error=None):
        self.verdict, self.error, self.calls = verdict, error, []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return StubCheck(self.verdict, kw["planned"])


@pytest.fixture(autouse=True)
def stub_seal():
    """The stubbed checks are not full TradeChecks; sealing is tested with the real check_trade below."""
    import egoquant_routes
    with patch.object(egoquant_routes, "seal_trade_context", lambda check: "sealed:" + check.verdict):
        yield


def agree(app_client, headers):
    for scope in ("ibkr_data", "trade_check"):
        body = {"scope": scope, "version": CONSENT_VERSION}
        assert app_client.post("/account/consent", json=body, headers=headers).status_code == 200


def run_check(app_client, stub, headers=A, planned=PLANNED, **kw):
    import egoquant_routes
    with patch.object(egoquant_routes.trade_check, "check_trade", stub, create=True):
        return app_client.post("/account/trade-check", json={**portfolio_request(), "planned": planned, **kw},
                               headers=headers)


def actions_of(client, headers=A):
    return [e["action"] for e in client.get("/account/log", headers=headers).json()["log"]]


def test_trade_check_needs_sign_in_consent_and_saved_history(app_client):
    stub = Stub()
    assert app_client.post("/account/trade-check", json={**portfolio_request(), "planned": PLANNED}).status_code == 401
    assert run_check(app_client, stub).status_code == 403                       # no consent yet
    consent(app_client, A)                                                      # ibkr_data only
    assert run_check(app_client, stub).status_code == 403
    agree(app_client, B)
    assert run_check(app_client, stub, headers=B).status_code == 404            # consent, but no trades connected
    assert stub.calls == []


def test_trade_check_uses_only_the_saved_history_and_never_the_request(app_client):
    agree(app_client, A)
    upload(app_client, A)
    stub = Stub()
    for extra in ({"current_holdings": {"VTI": 1.0}}, {"ibkr_report": "<x/>"}):
        assert run_check(app_client, stub, **extra).status_code == 422
    assert stub.calls == []


def test_trade_check_saves_the_verdict_and_logs_it(app_client, store):
    agree(app_client, A)
    upload(app_client, A)
    stub = Stub("allowed_with_warnings")
    out = run_check(app_client, stub)
    assert out.status_code == 200, out.text
    call = stub.calls[0]
    assert call["planned"].ticker == "VTI" and call["planned"].amount == 1500       # normalised, from the body
    assert call["holdings"] and all(v > 0 for v in call["holdings"].values())        # amounts from the saved trades
    saved = store.tables["trade_checks"]
    assert [r["verdict"] for r in saved] == ["allowed_with_warnings"] and saved[0]["user_id"] == "user-a"
    assert out.json()["check_id"] == saved[0]["id"] and out.json()["check"]["verdict"] == "allowed_with_warnings"
    assert {"trade_checked", "warning_shown"} <= set(actions_of(app_client))


def test_an_advisable_verdict_shows_no_warning(app_client):
    agree(app_client, A)
    upload(app_client, A)
    assert run_check(app_client, Stub("advisable")).status_code == 200
    actions = actions_of(app_client)
    assert "trade_checked" in actions and "warning_shown" not in actions


def test_a_trade_that_cannot_be_checked_is_a_clear_422(app_client, store):
    agree(app_client, A)
    upload(app_client, A)
    out = run_check(app_client, Stub(error=TradeCheckError("You hold less than that to sell.")))
    assert out.status_code == 422 and "hold less" in out.json()["detail"]
    assert store.tables.get("trade_checks", []) == []


@pytest.mark.parametrize("planned", [{"ticker": "VTI", "side": "short", "amount": 5},
                                     {"ticker": "VTI", "side": "buy", "amount": -1},
                                     {"ticker": "A;B", "side": "buy", "amount": 5},
                                     {"ticker": "VTI", "side": "buy", "amount": 5, "x": 1}])
def test_bad_planned_trades_are_refused(app_client, planned):
    agree(app_client, A)
    upload(app_client, A)
    stub = Stub()
    assert run_check(app_client, stub, planned=planned).status_code == 422
    assert stub.calls == []


# ---------------------------------------------------------------- "I made this trade"
def saved_check(store, user_id="user-a", side="buy"):
    store.tables.setdefault("trade_checks", []).append(
        {"id": CHECK_ID, "user_id": user_id, "verdict": "advisable",
         "planned": {"ticker": "VTI", "side": side, "amount": 1500}, "result": {}, "user_decision": None})
    return f"/account/trade-checks/{CHECK_ID}/decision"


def test_made_trade_is_recorded_from_the_saved_check_and_only_once(client, store):
    path = saved_check(store)
    assert client.post(path, json={"decision": "made"}, headers=A).status_code == 422          # price + quantity needed
    day = date.today().isoformat()
    # Ticker and side are not accepted from the request: they come from the saved check.
    sneaky = {"decision": "made", "quantity": 5, "price": 250, "ticker": "TSLA"}
    assert client.post(path, json=sneaky, headers=A).status_code == 422
    ok = client.post(path, json={"decision": "made", "quantity": 5, "price": 250, "trade_date": day}, headers=A)
    assert ok.status_code == 200, ok.text
    (trade,) = store.tables["trades"]
    assert (trade["symbol"], trade["side"], trade["source"], trade["user_id"]) == ("VTI", "buy", "user_recorded", "user-a")
    assert trade["quantity"] == 5 and trade["price"] == 250 and trade["cash_change"] == -1250
    assert store.tables["trade_checks"][0]["user_decision"] == "made"
    assert {"trade_recorded", "decision_recorded"} <= set(actions_of(client))
    assert client.post(path, json={"decision": "not_made"}, headers=A).status_code == 409      # decided already
    assert len(store.tables["trades"]) == 1


def test_a_recorded_sale_adds_cash(client, store):
    path = saved_check(store, side="sell")
    client.post(path, json={"decision": "made", "quantity": 2, "price": 100}, headers=A)
    assert store.tables["trades"][0]["cash_change"] == 200


def test_not_made_records_the_decision_but_no_trade(client, store):
    path = saved_check(store)
    assert client.post(path, json={"decision": "not_made"}, headers=A).status_code == 200
    assert store.tables.get("trades", []) == [] and store.tables["trade_checks"][0]["user_decision"] == "not_made"
    actions = actions_of(client)
    assert "decision_recorded" in actions and "trade_recorded" not in actions


def test_nobody_can_decide_on_someone_elses_check(client, store):
    path = saved_check(store, user_id="user-a")
    assert client.post(path, json={"decision": "made", "quantity": 1, "price": 1}, headers=B).status_code == 404
    assert client.post(path, json={"decision": "made", "quantity": 1, "price": 1}).status_code == 401
    assert store.tables.get("trades", []) == [] and store.tables["trade_checks"][0]["user_decision"] is None


@pytest.mark.parametrize("body", [{"decision": "made", "quantity": 0, "price": 5},
                                  {"decision": "made", "quantity": 1, "price": -5},
                                  {"decision": "made", "quantity": 1e30, "price": 5},
                                  {"decision": "made", "quantity": 1, "price": 5, "currency": "dollars"},
                                  {"decision": "maybe"}])
def test_bad_decisions_are_refused(client, store, body):
    path = saved_check(store)
    assert client.post(path, json=body, headers=A).status_code == 422
    assert store.tables.get("trades", []) == []


def test_a_trade_dated_in_the_future_is_refused(client, store):
    path = saved_check(store)
    future = (date.today() + timedelta(days=3)).isoformat()
    body = {"decision": "made", "quantity": 1, "price": 5, "trade_date": future}
    assert client.post(path, json=body, headers=A).status_code == 422
    assert store.tables.get("trades", []) == []


def test_the_check_id_must_be_a_uuid(client):
    path = "/account/trade-checks/1 OR 1=1/decision"
    assert client.post(path, json={"decision": "not_made"}, headers=A).status_code == 422


# ---------------------------------------------------------------- with the real check_trade (synthetic prices)
@pytest.mark.parametrize("planned", [{"ticker": "VTI", "side": "buy", "amount": 500},
                                     {"ticker": "ZZZZ", "side": "buy", "amount": 500}])
def test_the_real_check_runs_end_to_end_on_the_saved_history(app_client, store, planned):
    agree(app_client, A)
    upload(app_client, A)
    out = app_client.post("/account/trade-check", json={**portfolio_request(), "planned": planned}, headers=A)
    assert out.status_code in (200, 422), out.text            # 422 = a clear message (e.g. cannot be checked)
    if out.status_code == 200:
        check = out.json()["check"]
        assert check["verdict"] in ("advisable", "allowed_with_warnings", "not_advisable")
        assert store.tables["trade_checks"][0]["verdict"] == check["verdict"]
        if planned["ticker"] == "ZZZZ":
            assert check["verdict"] != "advisable"             # an unknown product can never be advisable
    else:
        assert "ZZZZ" not in out.text or planned["ticker"] == "ZZZZ"


# ---------------------------------------------------------------- visitors (no account): holdings from this request only
def visitor_check(app_client, stub, **kw):
    import egoquant_routes
    from tests.accounts.test_accounts import DATA
    sent = {"ibkr_report": (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8"), **kw}
    with patch.object(egoquant_routes.trade_check, "check_trade", stub, create=True):
        return app_client.post("/trade-check", json={**portfolio_request(**sent), "planned": PLANNED})


def test_a_visitor_can_check_a_trade_against_an_uploaded_history_and_nothing_is_stored(app_client, store):
    stub = Stub("allowed_with_warnings")
    out = visitor_check(app_client, stub)
    assert out.status_code == 200, out.text
    assert out.json()["saved"] is False and out.json()["check_id"] is None
    assert out.json()["check"]["verdict"] == "allowed_with_warnings"
    assert stub.calls[0]["holdings"] and all(v > 0 for v in stub.calls[0]["holdings"].values())
    assert store.tables == {}                                      # no account, no stored check, no log


def test_a_visitor_check_needs_the_history_in_the_request(app_client):
    stub = Stub()
    body = {**portfolio_request(), "planned": PLANNED}
    assert app_client.post("/trade-check", json=body).status_code == 422                   # no history at all
    held = {**portfolio_request(current_holdings={"VTI": 1.0}), "planned": PLANNED}
    assert app_client.post("/trade-check", json=held).status_code == 422                   # weights alone are not enough
    assert stub.calls == []


def test_a_visitor_check_refuses_an_impossible_trade_with_a_clear_message(app_client):
    out = visitor_check(app_client, Stub(error=TradeCheckError("You hold less than that to sell.")))
    assert out.status_code == 422 and "hold less" in out.json()["detail"]


def test_the_real_check_comes_back_sealed_for_the_chat(app_client):
    import egoquant_routes
    from egoquant.advisor.context import open_context
    from egoquant.advisor.trade_explain import seal_trade_context as real_seal
    from tests.accounts.test_accounts import DATA
    sent = portfolio_request(ibkr_report=(DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8"))
    with patch.object(egoquant_routes, "seal_trade_context", real_seal):
        out = app_client.post("/trade-check", json={**sent, "planned": PLANNED})
    assert out.status_code == 200, out.text
    facts = open_context(out.json()["trade_context"])               # what the chat will verify and explain
    assert facts["verdict"] == out.json()["check"]["verdict"]


# ---------------------------------------------------------------- trade-check-cash: cash need passed, direction returned
def test_the_routes_pass_the_investors_cash_need_to_the_check(app_client):
    agree(app_client, A)
    upload(app_client, A)
    stub = Stub()
    assert run_check(app_client, stub, min_cash_buffer=0.1, liquidity_need=0.05).status_code == 200
    assert stub.calls[0]["min_cash_buffer"] == 0.1 and stub.calls[0]["liquidity_need"] == 0.05
    visitor = Stub()
    assert visitor_check(app_client, visitor, min_cash_buffer=0.2).status_code == 200
    assert visitor.calls[0]["min_cash_buffer"] == 0.2 and visitor.calls[0]["liquidity_need"] == 0.0


def test_both_routes_return_the_direction(app_client, store):
    from tests.accounts.test_accounts import DATA
    agree(app_client, A)
    upload(app_client, A)
    signed_in = app_client.post("/account/trade-check", json={**portfolio_request(), "planned": PLANNED}, headers=A)
    sent = portfolio_request(ibkr_report=(DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8"))
    visitor = app_client.post("/trade-check", json={**sent, "planned": PLANNED})
    for out in (signed_in, visitor):
        assert out.status_code == 200, out.text
        check = out.json()["check"]
        assert check["direction"] in ("lowers_exposure", "raises_exposure", "mixed", "no_change")
        assert isinstance(check["direction_parts"], list)
