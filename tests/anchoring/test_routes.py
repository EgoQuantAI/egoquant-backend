"""Confirm, seal, proof, viewed and verify routes, with the accounts FakeStore (RLS) and the fake chain.

The full flow: confirm -> submitted -> confirmed -> proof -> verify match; plus the rules: rejected = 409 with
nothing stored or sent, a second confirm returns the same record, another user's report = 404, RPC down still
confirms (pending, recovered later), caps, the key never appears anywhere, /verify stores and logs nothing, and
everything is 503 / hidden when sealing is not configured."""

import copy
import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
pytest.importorskip("eth_account")              # skip without it; it is in requirements.txt
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from egoquant.accounts import routes as account_routes
from egoquant.accounts.store import AccountError, set_store
from egoquant.anchoring import routes as anchoring_routes
from egoquant.anchoring.client import FakeAnchorClient, SepoliaAnchorClient
from egoquant.anchoring.config import CONSENT_TEXT
from tests.accounts.test_accounts import TOKENS, A, B, FakeStore
from tests.anchoring.fake_rpc import FakeRpcSession
from tests.anchoring.test_record import SAVED

VERSION = "2026-10-20-v2"


class SealStore(FakeStore):
    """FakeStore + what migration 006 adds: uuid record ids, `at` timestamps, the unique constraints. Like Supabase's
    sign-up trigger, every user has a profile with a username."""

    def select(self, token, table, params):
        if table == "profiles" and not any(r["user_id"] == self.user(token).id for r in self.tables.get("profiles", [])):
            uid = self.user(token).id
            self.tables.setdefault("profiles", []).append({"user_id": uid, "username": f"{uid.replace('-', '_')}"[:20],
                                                          "avatar": None})
        return super().select(token, table, params)

    def insert(self, token, table, rows):
        now = datetime.now(timezone.utc).isoformat()
        if table == "advisory_records":
            have = self.tables.get(table, [])
            for r in rows:
                if any((h["report_id"], h["record_version"]) == (r["report_id"], r["record_version"])
                       or h["commitment_hex"] == r["commitment_hex"] for h in have):
                    raise AccountError(409, "That already exists.")
            rows = [{"id": str(uuid.uuid4()), **r} for r in rows]
        if table in ("operation_log", "record_seals"):
            rows = [{"at": now, **r} for r in rows]
        return super().insert(token, table, rows)


@pytest.fixture(autouse=True)
def small_document(monkeypatch):
    """These tests use a hand-written report the full renderer cannot draw; a small deterministic document stands in
    (tests/anchoring/test_final_news_paths.py and test_seal_v2.py seal real engine reports)."""
    from html import escape

    def document(report, *, username, report_id):
        return (f"<!doctype html><html><body><main><p>Sealed for: {escape(username)} · report {report_id[:8]}</p>"
                f"<p>{escape(json.dumps(report.get('allocation'), sort_keys=True))}</p></main></body></html>")
    monkeypatch.setattr(anchoring_routes, "sealed_document", document)


@pytest.fixture
def store():
    saved = dict(TOKENS)
    fake = SealStore()
    set_store(fake)
    account_routes._user_hits.clear()
    yield fake
    set_store(None)
    TOKENS.clear()
    TOKENS.update(saved)


@pytest.fixture
def chain():
    fake = FakeAnchorClient()
    anchoring_routes.set_anchor_client(fake)
    yield fake
    anchoring_routes.set_anchor_client()


@pytest.fixture
def api(store, chain):
    app = FastAPI()
    app.include_router(account_routes.router)
    app.include_router(anchoring_routes.router)
    return TestClient(app)


def saved_report(store, token="token-a", **changes) -> str:
    report = copy.deepcopy(SAVED)
    report.update(changes)
    row = store.insert(token, "reports", [{"summary": {"approved": report["approved"]}, "report": report}])[0]
    rid = row["id"]
    store.insert(token, "operation_log", [{"action": "questionnaire_submitted", "details": {"report_id": rid}},
                                          {"action": "report_generated", "details": {"report_id": rid}}])
    return rid


def confirm(api, rid, headers=A, version=VERSION):
    return api.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=headers)


def test_full_flow_confirm_submitted_confirmed_proof_verify(api, store, chain):
    rid = saved_report(store)
    assert api.post(f"/account/reports/{rid}/viewed", headers=A).json() == {"ok": True}
    out = confirm(api, rid)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["confirmed"] is True and body["already_confirmed"] is False
    assert body["seal"]["status"] == "submitted" and body["seal"]["tx_hash"].startswith("0x")
    assert body["seal"]["explorer_url"].endswith(body["seal"]["tx_hash"]) and body["seal"]["chain_id"] == 11155111
    assert chain.sent == [b"EGOQ1" + bytes.fromhex(body["commitment"])]       # nothing else goes on-chain
    chain.mine()
    seal = api.get(f"/account/reports/{rid}/seal", headers=A).json()
    assert seal["seal"]["status"] == "confirmed" and seal["seal"]["block_number"]
    proof = api.get(f"/account/reports/{rid}/proof", headers=A)
    assert 'filename="egoquant-seal-' in proof.headers["content-disposition"]
    p = proof.json()
    assert p["proof"] == "egoq-proof-2" and p["document"].startswith("<!doctype html>")
    assert p["tx_hash"] == body["seal"]["tx_hash"] and "report" not in p
    record = json.loads(p["record"])
    assert [r["action"] for r in record["operation_log"]] == [
        "questionnaire_submitted", "report_generated", "report_viewed", "plan_confirmed"]
    v = api.post("/verify", json=p).json()
    assert v["match"] is True and all(x for k, x in v["checks"].items() if k != "report") and v["format"] == "v2"
    actions = [r["action"] for r in store.tables["operation_log"]]
    assert actions.count("plan_confirmed") == 1 and actions.count("record_sealed") == 1
    consents = store.tables["consents"]
    assert consents == [{**consents[0], "scope": "record_seal", "version": VERSION}]


def test_tampered_proof_does_not_match(api, store, chain):
    rid = saved_report(store)
    confirm(api, rid)
    chain.mine()
    p = api.get(f"/account/reports/{rid}/proof", headers=A).json()
    record = json.loads(p["record"])
    record["allocation"][0]["weight_ppm"] += 1
    record["allocation"][1]["weight_ppm"] -= 1
    from egoquant.anchoring.canonical import canonical_text
    v = api.post("/verify", json={**p, "record": canonical_text(record)}).json()
    assert v["match"] is False and v["failed"] == "hash differs"


@pytest.mark.parametrize("changes", [{"approved": False}, {"violations": ["equity 55% > 40%"]}])
def test_rejected_report_409_nothing_stored_or_sent(api, store, chain, changes):
    rid = saved_report(store, **changes)
    out = confirm(api, rid)
    assert out.status_code == 409 and "approved" in out.json()["detail"]
    assert not store.tables.get("advisory_records") and not store.tables.get("record_seals")
    assert not store.tables.get("consents") and chain.sent == []


def test_report_saved_before_answers_409(api, store, chain):
    rid = saved_report(store)
    store.tables["reports"][0]["report"].pop("inputs")
    assert confirm(api, rid).status_code == 409 and chain.sent == []


def test_second_confirm_returns_the_same_record(api, store, chain):
    rid = saved_report(store)
    first = confirm(api, rid).json()
    second = confirm(api, rid).json()
    assert second["already_confirmed"] is True and second["record_id"] == first["record_id"]
    assert second["commitment"] == first["commitment"] and len(chain.sent) == 1
    assert len(store.tables["advisory_records"]) == 1


def test_other_users_report_is_404_everywhere(api, store, chain):
    rid = saved_report(store)
    assert confirm(api, rid, headers=B).status_code == 404
    confirm(api, rid)
    for path in ("seal", "proof"):
        assert api.get(f"/account/reports/{rid}/{path}", headers=B).status_code == 404
    assert api.post(f"/account/reports/{rid}/viewed", headers=B).status_code == 404
    assert api.get(f"/account/reports/{uuid.uuid4()}/seal", headers=A).status_code == 404


def test_needs_a_token_and_a_current_consent_version(api, store):
    rid = saved_report(store)
    assert confirm(api, rid, headers={}).status_code == 401
    assert confirm(api, rid, version="2020-01-01").status_code == 409
    assert api.post(f"/account/reports/{rid}/confirm", json={"consent_version": VERSION, "user_id": "user-b"},
                    headers=A).status_code == 422
    assert api.post("/account/reports/not-a-uuid/confirm", json={"consent_version": VERSION},
                    headers=A).status_code == 422


def test_rpc_down_still_confirms_then_recovers(api, store, chain):
    rid = saved_report(store)
    chain.down = True
    out = confirm(api, rid)
    assert out.status_code == 200 and out.json()["confirmed"] is True
    assert out.json()["seal"]["status"] == "pending" and out.json()["seal"]["error_kind"] == "rpc_down"
    assert api.get(f"/account/reports/{rid}/seal", headers=A).json()["seal"]["status"] == "pending"
    chain.down = False
    again = api.get(f"/account/reports/{rid}/seal", headers=A).json()
    assert again["seal"]["status"] == "submitted" and again["commitment"] == out.json()["commitment"]
    chain.mine()
    assert api.get(f"/account/reports/{rid}/seal", headers=A).json()["seal"]["status"] == "confirmed"
    assert len(chain.sent) == 1                        # the same commitment, sent once
    actions = [r["action"] for r in store.tables["operation_log"]]
    assert "seal_pending" in actions and "record_sealed" in actions


def test_polling_a_submitted_seal_writes_no_new_rows(api, store, chain):
    rid = saved_report(store)
    confirm(api, rid)
    n = len(store.tables["record_seals"])
    for _ in range(3):
        api.get(f"/account/reports/{rid}/seal", headers=A)
    assert len(store.tables["record_seals"]) == n and len(chain.sent) == 1


def test_per_user_cap_keeps_the_plan_confirmed(api, store, chain, monkeypatch):
    monkeypatch.setenv("EGOQ_SEAL_MAX_PER_USER_DAY", "2")
    rids = [saved_report(store) for _ in range(3)]
    outs = [confirm(api, r).json() for r in rids]
    assert [o["seal"]["status"] for o in outs] == ["submitted", "submitted", "pending"]
    assert outs[2]["confirmed"] is True and outs[2]["seal"]["error_kind"] == "cap" and len(chain.sent) == 2
    rid_b = saved_report(store, token="token-b")      # another user's cap is separate
    assert confirm(api, rid_b, headers=B).json()["seal"]["status"] == "submitted"


def test_global_cap(api, store, chain, monkeypatch):
    monkeypatch.setenv("EGOQ_SEAL_MAX_PER_DAY", "1")
    a, b = saved_report(store), saved_report(store, token="token-b")
    assert confirm(api, a).json()["seal"]["status"] == "submitted"
    assert confirm(api, b, headers=B).json()["seal"]["error_kind"] == "cap"


def test_viewed_logs_once(api, store):
    rid = saved_report(store)
    for _ in range(3):
        api.post(f"/account/reports/{rid}/viewed", headers=A)
    assert [r["action"] for r in store.tables["operation_log"]].count("report_viewed") == 1


def test_not_configured_hides_everything(store):
    anchoring_routes.set_anchor_client(None)
    try:
        app = FastAPI()
        app.include_router(anchoring_routes.router)
        api = TestClient(app)
        assert api.get("/anchoring/status").json() == {"enabled": False}
        rid = saved_report(store)
        assert confirm(api, rid).status_code == 503
        for path in ("seal", "proof"):
            assert api.get(f"/account/reports/{rid}/{path}", headers=A).status_code == 503
        assert api.post(f"/account/reports/{rid}/viewed", headers=A).status_code == 503
        assert api.get("/verify/tx/0x" + "ab" * 32).status_code == 503
        assert api.post("/verify", json={"proof": "egoq-proof-1"}).status_code == 503
        assert not store.tables.get("advisory_records")
    finally:
        anchoring_routes.set_anchor_client()


def test_status_when_enabled(api, chain):
    s = api.get("/anchoring/status").json()
    assert s["enabled"] is True and s["chain_id"] == 11155111 and s["sealing_addresses"][0] == chain.address
    assert s["consent_version"] == VERSION and s["consent_text"] == CONSENT_TEXT
    assert "can never be deleted" in s["consent_text"]


def test_verify_tx(api, store, chain):
    rid = saved_report(store)
    out = confirm(api, rid).json()
    chain.mine()
    tx = api.get(f"/verify/tx/{out['seal']['tx_hash']}").json()
    assert tx["found"] and tx["sender_ok"] and tx["prefix_ok"] and tx["commitment"] == out["commitment"]
    assert api.get("/verify/tx/0x" + "cd" * 32).json()["found"] is False
    for bad in ("0x" + "AB" * 32, "0x123", "abc"):
        assert api.get(f"/verify/tx/{bad}").status_code == 422


def test_verify_rpc_down_is_503_and_bad_proofs_422_without_echo(api, chain):
    chain.down = True
    proof = {"proof": "egoq-proof-1", "record": "{}", "salt": "00" * 32, "commitment": "00" * 32,
             "tx_hash": "0x" + "ab" * 32}
    assert api.post("/verify", json=proof).status_code == 503
    chain.down = False
    out = api.post("/verify", json={"proof": "egoq-proof-1", "record": "SECRET-ANSWERS", "salt": "zz"})
    assert out.status_code == 422 and "SECRET-ANSWERS" not in out.text


def test_verify_stores_and_logs_nothing(api, store, chain, caplog):
    rid = saved_report(store)
    confirm(api, rid)
    chain.mine()
    p = api.get(f"/account/reports/{rid}/proof", headers=A).json()
    before = copy.deepcopy(store.tables)
    with caplog.at_level(logging.DEBUG):
        api.post("/verify", json=p)
        api.get(f"/verify/tx/{p['tx_hash']}")
    assert store.tables == before
    assert p["salt"] not in caplog.text and "amount_invested" not in caplog.text


def test_verify_rate_limits_per_ip_and_global(api, monkeypatch):
    from egoquant.anchoring import config
    monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 3)
    codes = [api.get("/verify/tx/0x" + "ab" * 32).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]
    anchoring_routes.set_anchor_client(FakeAnchorClient())
    monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 100)
    monkeypatch.setattr(config, "VERIFY_GLOBAL_PER_MINUTE", 2)
    codes = [api.get("/verify/tx/0x" + "ab" * 32).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_key_never_appears_in_any_log_or_response(store, caplog):
    """A real signing client (fresh key, fake RPC node): confirm, RPC failure, retry, receipt, proof, verify."""
    key = Account.create().key.hex()
    raw = key.removeprefix("0x").lower()
    node = FakeRpcSession()
    urls = ("https://rpc-a.invalid", "https://rpc-b.invalid")
    anchoring_routes.set_anchor_client(SepoliaAnchorClient(key, rpc_urls=urls, session=node))
    try:
        app = FastAPI()
        app.include_router(account_routes.router)
        app.include_router(anchoring_routes.router)
        api = TestClient(app)
        bodies = []
        with caplog.at_level(logging.DEBUG):
            rid = saved_report(store)
            node.down = set(urls)
            bodies.append(confirm(api, rid).text)
            node.down = set()
            bodies.append(api.get(f"/account/reports/{rid}/seal", headers=A).text)
            node.mine()
            bodies.append(api.get(f"/account/reports/{rid}/seal", headers=A).text)
            proof = api.get(f"/account/reports/{rid}/proof", headers=A)
            bodies.append(proof.text)
            bodies.append(api.post("/verify", json=proof.json()).text)
            bodies.append(api.get("/anchoring/status").text)
        assert json.loads(bodies[2])["seal"]["status"] == "confirmed"
        assert json.loads(bodies[4])["match"] is True
        everything = " ".join(bodies + [caplog.text, store.everything()]).lower()
        assert raw not in everything
        for host in ("rpc-a.invalid", "rpc-b.invalid"):                     # no RPC URL in any log or body
            assert host not in caplog.text and host not in " ".join(bodies)
    finally:
        anchoring_routes.set_anchor_client()
