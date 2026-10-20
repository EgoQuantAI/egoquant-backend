"""What-If on a saved report: /account/portfolio keeps the What-If base facts the build sealed, and
/account/reports/{id}/contexts re-seals them (with the report's id, as of the build, for the owner only). Invented data."""

import copy
import json
from datetime import datetime, timezone

import egoquant_routes
from egoquant.advisor.context import open_context
from egoquant.agents.what_if import open_what_if_context
from egoquant.anchoring.canonical import report_sha256
from tests.accounts.test_accounts import A, B, app_client, client, consent, portfolio_request, store, upload  # noqa: F401


def build(app_client):  # noqa: F811
    consent(app_client, A)
    upload(app_client, A)
    out = app_client.post("/account/portfolio", json=portfolio_request(), headers=A)
    assert out.status_code == 200, out.text
    return out.json()


def fresh(store):  # noqa: F811
    row = store.tables["reports"][0]
    row["created_at"] = datetime.now(timezone.utc).isoformat()      # the fake store's fixed date may be old
    return row


def test_the_saved_row_keeps_exactly_the_facts_the_build_sealed(app_client, store):  # noqa: F811
    data = build(app_client)
    sealed = open_what_if_context(data["what_if_context"])
    kept = store.tables["reports"][0]["report"]["what_if_facts"]
    assert kept == {k: v for k, v in sealed.items() if k != "report_id"}
    assert sealed["report_id"] == data["report_id"] and kept["user_id"] == "user-a"


def test_contexts_reseal_them_with_the_report_id_as_of_the_build(app_client, store):  # noqa: F811
    data = build(app_client)
    row = fresh(store)
    body = app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=A).json()
    assert open_context(body["what_if_context"]) == open_what_if_context(data["what_if_context"])
    assert body["what_if_note"] is None
    assert open_context(body["chat_context"]) == open_context(data["chat_context"])   # the chat facts never see them
    assert row["report"]["what_if_facts"]                             # the GET changed nothing in the row


def test_another_user_gets_404_and_no_token_is_echoed(app_client, store):  # noqa: F811
    data = build(app_client)
    fresh(store)
    r = app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=B)
    assert r.status_code == 404
    ok = app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=A)
    assert "token-a" not in ok.text and "token-b" not in r.text


def test_an_old_row_without_the_facts_gets_none_and_says_why(app_client, store):  # noqa: F811
    data = build(app_client)
    row = fresh(store)
    row["report"].pop("what_if_facts")
    body = app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=A).json()
    assert body["what_if_context"] is None and body["what_if_note"] == "older_report"
    assert body["chat_context"]                                       # the chat still works


def test_facts_that_do_not_fit_the_row_are_left_out_and_reported(app_client, store, monkeypatch):  # noqa: F811
    real = egoquant_routes._MAX_SAVED_REPORT_BYTES
    monkeypatch.setattr(egoquant_routes, "_MAX_SAVED_REPORT_BYTES", 10**9)
    build(app_client)
    size = len(json.dumps(store.tables["reports"][0]["report"]))
    small = len(json.dumps({**store.tables["reports"][0]["report"], "what_if_facts": None}))
    store.tables["reports"].clear()
    monkeypatch.setattr(egoquant_routes, "_MAX_SAVED_REPORT_BYTES", (size + small) // 2)   # fits without, not with
    data = app_client.post("/account/portfolio", json=portfolio_request(), headers=A).json()
    row = fresh(store)
    assert row["report"]["what_if_facts"] is None and real > 0
    body = app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=A).json()
    assert body["what_if_context"] is None and body["what_if_note"] == "not_kept"
    assert data["what_if_context"]                                    # the live build still has its own


def test_the_seal_fingerprint_covers_the_stored_row_and_reading_contexts_does_not_change_it(app_client, store):  # noqa: F811
    data = build(app_client)
    row = fresh(store)
    before = report_sha256(row["report"])                             # canonical: no NaN, no huge integer in the facts
    app_client.get(f"/account/reports/{data['report_id']}/contexts", headers=A)
    assert report_sha256(row["report"]) == before
    old = copy.deepcopy(row["report"])
    old.pop("what_if_facts")
    assert report_sha256(old) != before                               # the facts are part of what a seal covers
    assert report_sha256(copy.deepcopy(old)) == report_sha256(old)    # and an older row's fingerprint is its own
