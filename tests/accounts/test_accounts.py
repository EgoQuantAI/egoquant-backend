"""User accounts: access control (IDOR), consent, IBKR import, delete-my-data, and the Supabase client itself.

Supabase is replaced by FakeStore, which enforces the same Row-Level Security rule as
supabase/migrations/001_accounts.sql (a token reads and writes only rows whose user_id is its own user).
supabase/tests/rls_isolation.sql proves the real policies inside Supabase. Invented data only.
"""

import dataclasses
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from egoquant.accounts import routes as account_routes
from egoquant.accounts.routes import CONSENT_VERSION
from egoquant.accounts.store import AccountError, SupabaseStore, User, set_store

DATA = Path(__file__).parent.parent / "agent_b" / "data"
TOKENS = {"token-a": User("user-a"), "token-b": User("user-b")}
A, B = {"Authorization": "Bearer token-a"}, {"Authorization": "Bearer token-b"}
FAKE_FLEX_TOKEN = "FAKEflex0123456789"


class FakeStore:
    """In-memory Supabase with Row-Level Security: user_id must be the token's own user, always."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.usernames = {"taken_name"}
        self.metadata_updates: list[dict] = []
        self.next_id = 1

    def user(self, token):
        if token not in TOKENS:
            raise AccountError(401, "Your session has expired. Please sign in again.")
        return TOKENS[token]

    def _match(self, row, params):
        for key, cond in params.items():
            if key in ("select", "order", "limit"):
                continue
            op, _, value = cond.partition(".")
            have = str(row.get(key))
            if op == "eq" and have != value:
                return False
            if op == "in" and have not in value.strip("()").split(","):
                return False
            if op == "gt" and not have > value:
                return False
        return True

    def _rls(self, token, table):                  # the Row-Level Security policy
        uid = self.user(token).id
        return [r for r in self.tables.get(table, []) if r["user_id"] == uid]

    def select(self, token, table, params):
        return [dict(r) for r in self._rls(token, table) if self._match(r, params)]

    def insert(self, token, table, rows):
        uid = self.user(token).id
        if any(r.get("user_id", uid) != uid for r in rows):
            raise AccountError(403, "Not allowed.")
        out = []
        for r in rows:
            row_id = str(uuid.uuid4()) if table in ("reports", "trade_checks") else self.next_id
            row = {"id": row_id, "user_id": uid, "created_at": "2026-10-19T00:00:00+00:00",
                   "expires_at": "2099-01-01T00:00:00+00:00", **r}
            self.next_id += 1
            self.tables.setdefault(table, []).append(row)
            out.append(row)
        return out

    def update(self, token, table, params, values):
        rows = [r for r in self._rls(token, table) if self._match(r, params)]
        for r in rows:
            r.update(values)
        return rows

    def delete(self, token, table, params):
        doomed = [r for r in self._rls(token, table) if self._match(r, params)]
        self.tables[table] = [r for r in self.tables.get(table, []) if r not in doomed]
        return len(doomed)

    def rpc(self, token, name, args):
        if name == "username_available":
            return args["name"] not in self.usernames
        uid = self.user(token).id
        if name in ("delete_my_data", "delete_my_account"):
            for table in self.tables:
                self.tables[table] = [r for r in self.tables[table] if r["user_id"] != uid]
            if name == "delete_my_account":
                TOKENS.pop(next(t for t, u in TOKENS.items() if u.id == uid))
            return None
        raise AssertionError(name)

    def update_user_data(self, token, data):
        user = self.user(token)                      # as the user, like PUT /auth/v1/user
        self.metadata_updates.append(data)
        if data.get("eq_consents", "keep") is None:
            TOKENS[token] = dataclasses.replace(user, signup_consents=())

    def everything(self) -> str:
        return repr(self.tables)


@pytest.fixture
def store():
    saved = dict(TOKENS)
    fake = FakeStore()
    set_store(fake)
    account_routes._user_hits.clear()
    account_routes._username_hits.clear()
    yield fake
    set_store(None)
    TOKENS.clear()
    TOKENS.update(saved)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(account_routes.router)
    return TestClient(app)


def consent(client, headers):
    return client.post("/account/consent", json={"scope": "ibkr_data", "version": CONSENT_VERSION}, headers=headers)


def upload(client, headers, name="ibkr_cautious_seller.xml"):
    return client.post("/account/trades/import", json={"ibkr_report": (DATA / name).read_text(encoding="utf-8")},
                       headers=headers)


# ---------------------------------------------------------------- authentication
@pytest.mark.parametrize("method, path", [("get", "/account/me"), ("get", "/account/trades"), ("get", "/account/log"),
                                          ("post", "/account/consent"), ("post", "/account/trades/import"),
                                          ("delete", "/account/data"), ("delete", "/account")])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer forged"}, {"Authorization": "token-a"}])
def test_every_endpoint_needs_a_verified_token(client, method, path, headers):
    bodies = {"/account/consent": {"scope": "ibkr_data", "version": CONSENT_VERSION},
              "/account/trades/import": {"ibkr_report": "x"}}
    kwargs = {"json": bodies[path]} if method == "post" else {}
    assert getattr(client, method)(path, headers=headers, **kwargs).status_code == 401


# ---------------------------------------------------------------- access control (IDOR)
def test_user_b_never_sees_or_changes_user_a_s_data(client, store):
    assert consent(client, A).status_code == 200
    assert upload(client, A).status_code == 200
    a_trades = client.get("/account/trades", headers=A).json()["trades"]
    assert a_trades
    assert client.get("/account/trades", headers=B).json()["trades"] == []
    assert client.get("/account/log", headers=B).json()["log"] == []
    assert client.get("/account/me", headers=B).json()["n_trades"] == 0
    assert client.delete("/account/data", headers=B).status_code == 200        # B erases only B
    assert client.get("/account/trades", headers=A).json()["trades"] == a_trades


def test_no_endpoint_accepts_a_user_id(client, store):
    smuggled = {"scope": "ibkr_data", "version": CONSENT_VERSION, "user_id": "user-a"}
    assert client.post("/account/consent", json=smuggled, headers=B).status_code == 422
    assert not store.select("token-a", "consents", {})


def test_a_row_for_someone_else_is_refused_by_the_store(store):
    with pytest.raises(AccountError) as err:
        store.insert("token-b", "trades", [{"user_id": "user-a", "symbol": "VTI"}])
    assert err.value.status == 403


# ---------------------------------------------------------------- consent, import, privacy
def test_importing_needs_consent_first(client):
    assert upload(client, A).status_code == 403
    assert client.post("/account/consent", json={"scope": "ibkr_data", "version": "old"}, headers=A).status_code == 409


def test_import_keeps_only_what_the_analysis_needs_and_replaces_the_previous_one(client, store):
    consent(client, A)
    first = upload(client, A).json()
    assert first["source"] == "ibkr_report" and first["n_trades"] > 0
    upload(client, A, "ibkr_steady_holder.csv")
    trades = client.get("/account/trades", headers=A).json()["trades"]
    assert len(trades) == upload(client, A, "ibkr_steady_holder.csv").json()["n_trades"]   # replaced, not added
    stored = store.everything()
    assert "U0000000" not in stored                                         # no account number kept
    assert "description" not in stored and "accountId" not in stored
    log = client.get("/account/log", headers=A).json()["log"]
    assert {e["action"] for e in log} == {"consent_given", "data_connected"}


def test_a_flex_token_is_used_once_and_never_stored_or_logged(client, store, monkeypatch):
    consent(client, A)
    monkeypatch.setattr("egoquant.accounts.ibkr_input.fetch_flex_report",
                        lambda token, query_id, **_: (DATA / "ibkr_cautious_seller.xml").read_text(encoding="utf-8"))
    r = client.post("/account/trades/import", json={"ibkr_flex": {"token": FAKE_FLEX_TOKEN, "query_id": "42"}},
                    headers=A)
    assert r.status_code == 200 and r.json()["source"] == "ibkr_flex"
    assert FAKE_FLEX_TOKEN not in r.text and FAKE_FLEX_TOKEN not in store.everything()


def test_import_needs_exactly_one_source(client):
    consent(client, A)
    assert client.post("/account/trades/import", json={}, headers=A).status_code == 422


def test_delete_my_data_and_delete_my_account(client, store):
    consent(client, A)
    upload(client, A)
    assert client.delete("/account/data", headers=A).json() == {"deleted": True}
    assert client.get("/account/trades", headers=A).json()["trades"] == []
    assert client.delete("/account", headers=A).status_code == 200
    assert client.get("/account/me", headers=A).status_code == 401           # the account is gone


def test_username_availability_is_yes_or_no_only(client):
    assert client.get("/account/username-available", params={"name": "taken_name"}).json() == {"available": False}
    assert client.get("/account/username-available", params={"name": "fresh_name"}).json() == {"available": True}
    assert client.get("/account/username-available", params={"name": "Bad Name!"}).status_code == 422


def test_per_user_rate_limit(client):
    for _ in range(account_routes._USER_LIMIT):
        assert client.get("/account/me", headers=A).status_code == 200
    assert client.get("/account/me", headers=A).status_code == 429
    assert client.get("/account/me", headers=B).status_code == 200            # B has its own budget


# ---------------------------------------------------------------- the real Supabase client (HTTP mocked)
class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload = status, payload
        self.content = b"" if payload is None and not text else b"x"
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_supabase_client_always_acts_as_the_user_and_never_passes_supabase_text_through():
    session = FakeSession(FakeResponse(200, {"id": "user-a", "email": "a@example.invalid"}),
                          FakeResponse(200, [{"id": 1}]),
                          FakeResponse(400, None, text='{"message": "bad value \'secret-input\'"}'))
    store = SupabaseStore("https://example.supabase.co", "anon-public-key", session=session)
    assert store.user("user-jwt").id == "user-a"
    assert store.user("user-jwt").id == "user-a"                                # cached: one auth call only
    store.select("user-jwt", "trades", {"user_id": "eq.user-a"})
    for _, _, kw in session.calls:
        assert kw["headers"]["Authorization"] == "Bearer user-jwt"              # always the user's own token
        assert kw["headers"]["apikey"] == "anon-public-key"
    with pytest.raises(AccountError) as err:
        store.select("user-jwt", "trades", {})
    assert err.value.status == 502 and "secret-input" not in str(err.value)
    assert all("user-jwt" not in key for key in store._users)                  # cache keys are hashes


def test_supabase_client_refuses_plain_http_and_maps_auth_errors():
    with pytest.raises(AccountError):
        SupabaseStore("http://example.supabase.co", "k")
    store = SupabaseStore("https://example.supabase.co", "k", session=FakeSession(FakeResponse(401, {})))
    with pytest.raises(AccountError) as err:
        store.user("expired")
    assert err.value.status == 401


# ---------------------------------------------------------------- reports built from the saved history (O3)
@pytest.fixture
def app_client(store):
    import egoquant_routes
    app = FastAPI()
    app.include_router(egoquant_routes.router)                  # /portfolio, /account/portfolio and /account/...
    with patch.dict(os.environ, {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off"}):
        egoquant_routes._portfolio_timestamps.clear()
        yield TestClient(app)


def portfolio_request(**kw):
    from egoquant.demo.personas import PERSONAS
    from tests.test_egoquant_routes import portfolio_body
    return portfolio_body(next(p for p in PERSONAS.values() if p.expected_class == "C4"), **kw)


def test_a_report_is_built_from_the_saved_history_and_saved_for_its_owner_only(app_client, store):
    consent(app_client, A)
    upload(app_client, A)
    built = app_client.post("/account/portfolio", json=portfolio_request(), headers=A)
    assert built.status_code == 200, built.text
    data = built.json()
    assert data["trading_history"]["source"] == "account" and data["report_id"]
    assert any("behavioural" in c["name"] for c in data["report"]["limits"])  # the saved sells reached Agent B
    rid = data["report_id"]
    assert [r["id"] for r in app_client.get("/account/reports", headers=A).json()["reports"]] == [rid]
    assert app_client.get(f"/account/reports/{rid}", headers=A).json()["report"]["approved"] == data["approved"]
    # User B: nothing listed, A's id is "not found", and B cannot delete it.
    assert app_client.get("/account/reports", headers=B).json()["reports"] == []
    assert app_client.get(f"/account/reports/{rid}", headers=B).status_code == 404
    assert app_client.delete(f"/account/reports/{rid}", headers=B).status_code == 404
    assert app_client.get(f"/account/reports/{rid}", headers=A).status_code == 200
    actions = [e["action"] for e in app_client.get("/account/log", headers=A).json()["log"]]
    assert "report_generated" in actions
    assert "chat_context" not in store.everything()                    # the sealed chat facts are not stored


def test_account_portfolio_needs_sign_in_and_uses_only_the_saved_history(app_client):
    assert app_client.post("/account/portfolio", json=portfolio_request()).status_code == 401
    sent = portfolio_request(ibkr_report=(DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8"))
    assert app_client.post("/account/portfolio", json=sent, headers=A).status_code == 422
    empty = app_client.post("/account/portfolio", json=portfolio_request(), headers=B)   # no trades yet: still a report
    assert empty.status_code == 200 and empty.json()["trading_history"] is None and empty.json()["report_id"]


def test_report_ids_must_be_uuids(app_client):
    assert app_client.get("/account/reports/1 OR 1=1", headers=A).status_code == 422


# ---------------------------------------------------------------- which Supabase key the server accepts
def _jwt(role):
    import base64
    import json
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")   # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc({'role': role})}.sig"


@pytest.mark.parametrize("key, privileged", [("sb_secret_abc", True), (_jwt("service_role"), True),
                                             (_jwt("anon"), False), ("sb_publishable_abc", False), ("plain", False)])
def test_only_the_public_supabase_key_is_accepted(key, privileged):
    from egoquant.accounts.store import _is_privileged_key
    assert _is_privileged_key(key) is privileged


def test_a_service_role_key_is_never_used(monkeypatch):
    set_store(None)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_KEY", _jwt("service_role"))
    from egoquant.accounts.store import get_store
    with pytest.raises(AccountError):
        get_store()
    monkeypatch.setenv("SUPABASE_KEY", "sb_publishable_ok")
    assert get_store() is not None
    set_store(None)


# ---------------------------------------------------------------- onboarding saved in the account
ONBOARDING = {"answers": {"questionnaire_version": "v1", "choices": {"T1": "a"}}, "profile": {"c_level": "C3"},
              "interests": {"summary": ["Stocks"]}, "intake": {"horizon_years": 10}, "builtAt": "2026-10-20T10:00:00Z",
              "result": {"report_id": str(uuid.uuid4()), "chat_context": "sealed.sig"}, "savedAt": "2026-10-20T10:00:00Z"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer forged"}])
def test_onboarding_needs_a_verified_token(client, headers):
    assert client.get("/account/onboarding", headers=headers).status_code == 401
    assert client.put("/account/onboarding", json={"state": ONBOARDING}, headers=headers).status_code == 401


def test_onboarding_is_saved_replaced_and_private(client, store):
    assert client.get("/account/onboarding", headers=A).json()["state"] is None
    assert client.put("/account/onboarding", json={"state": ONBOARDING}, headers=A).status_code == 200
    assert client.get("/account/onboarding", headers=A).json()["state"]["profile"] == {"c_level": "C3"}
    later = {**ONBOARDING, "profile": {"c_level": "C4"}, "savedAt": "2026-10-20T11:00:00Z"}
    assert client.put("/account/onboarding", json={"state": later}, headers=A).status_code == 200
    assert len(store.tables["onboarding"]) == 1                       # one row per user, replaced
    assert client.get("/account/onboarding", headers=A).json()["state"]["profile"] == {"c_level": "C4"}
    assert client.get("/account/onboarding", headers=B).json()["state"] is None   # never someone else's


def test_onboarding_refuses_unknown_fields_and_huge_bodies(client):
    odd = client.put("/account/onboarding", json={"state": {**ONBOARDING, "user_id": "user-b"}}, headers=A)
    assert odd.status_code == 422
    bad_ref = client.put("/account/onboarding", json={"state": {**ONBOARDING, "result": {"report_id": "x"}}}, headers=A)
    assert bad_ref.status_code == 422
    huge = {**ONBOARDING, "answers": {"blob": "x" * 210_000}}
    assert client.put("/account/onboarding", json={"state": huge}, headers=A).status_code == 413


def test_delete_my_data_erases_onboarding(client, store):
    client.put("/account/onboarding", json={"state": ONBOARDING}, headers=A)
    assert client.delete("/account/data", headers=A).status_code == 200
    assert client.get("/account/onboarding", headers=A).json()["state"] is None


# ---------------------------------------------------------------- name and consents chosen at sign-up
def signed_up(token="token-a", **kw):
    TOKENS[token] = dataclasses.replace(TOKENS[token], **kw)


def test_the_consent_version_is_public_and_carries_no_data(client):
    assert client.get("/account/consent-version").json() == {"version": CONSENT_VERSION,
                                                             "scopes": ["ibkr_data", "trade_check"]}


def test_the_name_from_sign_up_is_returned_to_its_owner_only(client):
    signed_up(name="Ada Lovelace")
    assert client.get("/account/profile", headers=A).json()["name"] == "Ada Lovelace"
    assert client.get("/account/me", headers=A).json()["name"] == "Ada Lovelace"
    assert client.get("/account/profile", headers=B).json()["name"] is None


def test_consents_ticked_at_sign_up_are_recorded_once_and_never_after_delete_my_data(client, store):
    signed_up(signup_consents=(("ibkr_data", CONSENT_VERSION), ("trade_check", CONSENT_VERSION)))
    assert client.get("/account/me", headers=A).json()["consents"] == {"ibkr_data": False, "trade_check": False}
    first = client.post("/account/signup-consents", headers=A).json()
    assert first == {"recorded": ["ibkr_data", "trade_check"]}
    assert store.metadata_updates == [{"eq_consents": None}]           # removed from the metadata first
    assert client.get("/account/me", headers=A).json()["consents"] == {"ibkr_data": True, "trade_check": True}
    log = client.get("/account/log", headers=A).json()["log"]
    assert sorted(r["details"]["scope"] for r in log if r["action"] == "consent_given") == ["ibkr_data", "trade_check"]
    assert client.post("/account/signup-consents", headers=A).json() == {"recorded": []}   # once only
    client.delete("/account/data", headers=A)
    assert client.post("/account/signup-consents", headers=A).json() == {"recorded": []}   # never re-applied
    assert client.get("/account/me", headers=A).json()["consents"] == {"ibkr_data": False, "trade_check": False}
    assert client.get("/account/me", headers=B).json()["consents"] == {"ibkr_data": False, "trade_check": False}


def test_a_sign_up_consent_for_old_text_or_an_unknown_scope_is_not_recorded(client, store):
    signed_up(signup_consents=(("ibkr_data", "2000-01-01"), ("record_seal", CONSENT_VERSION),
                               ("trade_check", CONSENT_VERSION)))
    assert client.post("/account/signup-consents", headers=A).json() == {"recorded": ["trade_check"]}
    assert store.metadata_updates == [{"eq_consents": None}]


def test_sign_up_consents_need_a_verified_token(client):
    assert client.post("/account/signup-consents").status_code == 401
    assert client.post("/account/signup-consents", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_the_supabase_client_reads_only_our_metadata_and_writes_it_as_the_user():
    session = FakeSession(
        FakeResponse(200, {"id": "user-a", "user_metadata": {
            "name": "  Ada   Lovelace " + "x" * 60, "role": "admin",
            "eq_consents": {"ibkr_data": CONSENT_VERSION, "trade_check": 7}}}),
        FakeResponse(200, {"id": "user-a"}),
        FakeResponse(200, {"id": "user-a", "user_metadata": {}}),
    )
    store = SupabaseStore("https://example.supabase.co", "anon-public-key", session=session)
    user = store.user("user-jwt")
    assert user.name.startswith("Ada Lovelace") and len(user.name) == 40
    assert user.signup_consents == (("ibkr_data", CONSENT_VERSION),)  # a non-text version is dropped
    store.update_user_data("user-jwt", {"eq_consents": None})
    method, url, kw = session.calls[1]
    assert (method, url, kw["json"]) == ("PUT", "https://example.supabase.co/auth/v1/user", {"data": {"eq_consents": None}})
    assert kw["headers"]["Authorization"] == "Bearer user-jwt"
    assert store.user("user-jwt").signup_consents == ()               # the cached user was dropped
    assert len(session.calls) == 3
    assert User.from_auth({"id": "x", "user_metadata": "junk"}) == User("x")


# ---------------------------------------------------------------- /account/me reads in parallel (Bideci's AI, 20 Oct)
def test_me_reports_each_consent_like_has_consent(client, store):
    """One consents read for every scope gives the same answer as has_consent: current version and not expired only."""
    assert client.get("/account/me", headers=A).json()["consents"] == {"ibkr_data": False, "trade_check": False}
    assert consent(client, A).status_code == 200
    store.tables["consents"].append({"id": 990, "user_id": "user-a", "scope": "trade_check", "version": "2000-01-01",
                                     "expires_at": "2099-01-01T00:00:00+00:00"})          # older text: does not count
    store.tables["consents"].append({"id": 991, "user_id": "user-a", "scope": "trade_check", "version": CONSENT_VERSION,
                                     "expires_at": "2000-01-01T00:00:00+00:00"})          # expired: does not count
    me = client.get("/account/me", headers=A).json()
    user = account_routes.get_store().user("token-a")
    assert me["consents"] == {s: account_routes.has_consent("token-a", user, s) for s in account_routes.CONSENT_SCOPES}
    assert me["consents"] == {"ibkr_data": True, "trade_check": False}
    assert client.get("/account/me", headers=B).json()["consents"] == {"ibkr_data": False, "trade_check": False}   # A's stay A's


def test_me_reads_profile_trades_reports_and_consents_once_each(client, store):
    calls: list[str] = []
    real = store.select
    store.select = lambda token, table, params: calls.append(table) or real(token, table, params)
    assert client.get("/account/me", headers=A).status_code == 200
    assert sorted(calls) == ["consents", "profiles", "reports", "trades"]
