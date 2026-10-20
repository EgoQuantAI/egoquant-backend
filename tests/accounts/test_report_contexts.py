"""A saved report gets the Advisor's sealed facts again (GET /account/reports/{id}/contexts): the same facts the build
sealed, sealed as of the build, only for the owner. Invented data only."""

import json
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from egoquant.advisor import build_portfolio_facts
from egoquant.advisor.context import MAX_AGE_SECONDS, _unb64, open_context
from tests.accounts.test_accounts import A, B, client, store  # noqa: F401

RID = "11111111-2222-4333-8444-555555555555"
OUT = {   # a /portfolio response as the account route stores it (no html, chat_context, report_id, what_if_context)
    "approved": True, "status": "optimal", "c_level": "C3", "as_of": "2026-10-16", "data_source": "historical",
    "violations": [], "checks": [{"name": "equity cap", "passed": True, "detail": "40% of 70%"}],
    "game_note": None, "goal": None, "briefing_context": "sealed-briefing-inputs",
    "report": {"headline": "A balanced portfolio.", "equity_weight": 0.4, "ex_ante_vol": 0.09, "worst_loss": -0.18,
               "allocation": [{"asset_id": "VTI", "weight": 0.4}, {"asset_id": "BND", "weight": 0.6}]},
}
ANSWERS = {"questionnaire_version": "v1", "amount_invested": 10000}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def saved(store):
    built = datetime.now(timezone.utc) - timedelta(days=2)
    store.tables["reports"] = [{"id": RID, "user_id": "user-a", "created_at": iso(built),
                                "report": {**OUT, "inputs": {"answers": ANSWERS}}}]
    return built


def iat(token: str) -> int:
    return json.loads(zlib.decompress(_unb64(token.split(".")[0])))["iat"]


def test_the_owner_gets_the_facts_the_build_sealed_sealed_as_of_the_build(client, saved):
    r = client.get(f"/account/reports/{RID}/contexts", headers=A)
    assert r.status_code == 200
    body = r.json()
    assert open_context(body["chat_context"]) == json.loads(json.dumps(build_portfolio_facts(OUT)))
    assert iat(body["chat_context"]) == int(saved.timestamp())          # the age counts from the build, not from now
    assert body["answers"] == ANSWERS and body["briefing_context"] == "sealed-briefing-inputs"
    assert body["what_if_context"] is None and body["expired"] is False   # needs the build's run: not stored


def test_another_user_gets_404_like_a_missing_report(client, saved):
    assert client.get(f"/account/reports/{RID}/contexts", headers=B).status_code == 404
    assert client.get("/account/reports/99999999-2222-4333-8444-555555555555/contexts", headers=A).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer forged"}])
def test_a_verified_token_is_required_and_never_echoed(client, saved, headers):
    r = client.get(f"/account/reports/{RID}/contexts", headers=headers)
    assert r.status_code == 401 and "forged" not in r.text
    ok = client.get(f"/account/reports/{RID}/contexts", headers=A)
    assert "token-a" not in ok.text


def test_a_report_older_than_the_context_age_is_not_discussed(client, store):
    old = datetime.now(timezone.utc) - timedelta(seconds=MAX_AGE_SECONDS + 3600)
    store.tables["reports"] = [{"id": RID, "user_id": "user-a", "created_at": iso(old), "report": dict(OUT)}]
    body = client.get(f"/account/reports/{RID}/contexts", headers=A).json()
    assert body["chat_context"] is None and body["expired"] is True


def test_an_old_row_without_inputs_still_gets_its_facts(client, store):
    store.tables["reports"] = [{"id": RID, "user_id": "user-a", "created_at": iso(datetime.now(timezone.utc)),
                                "report": dict(OUT)}]
    body = client.get(f"/account/reports/{RID}/contexts", headers=A).json()
    assert body["answers"] is None and open_context(body["chat_context"])["c_level"] == "C3"


def test_an_unreadable_row_gives_no_chat_never_a_500(client, store):
    store.tables["reports"] = [{"id": RID, "user_id": "user-a", "created_at": "not a date", "report": {"report": None}}]
    body = client.get(f"/account/reports/{RID}/contexts", headers=A).json()
    assert body["chat_context"] is None and body["expired"] is False
