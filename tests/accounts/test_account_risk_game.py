"""The Risk Game on the signed-in routes (docs/RISK_GAME_BLEND.md): /account/portfolio, /account/trade-check, the saved report,
its sealed contexts, delete-my-data, and the on-chain record (egoquant/anchoring). Supabase is the FakeStore of test_accounts.py.

What is pinned:
  * the saved report's `inputs.answers` has `risk_game` ONLY when a game was sent (the keys are otherwise exactly the old ones);
  * the saved row survives GET, belongs to its owner, and its category is the BLENDED one in every place it appears
    (report, summary, chat and what-if contexts, the re-sealed contexts of the saved report, the trade check);
  * a game replayed later changes the category of NEW builds only (the old row keeps what it said);
  * a game in two places with different values, or a hostile game, saves nothing and echoes nothing;
  * the record sealed on-chain is byte-identical for a report saved without a game, and carries the game (and not the time it was
    played) for one saved with a game; delete-my-data erases the saved game with the rest.
SYNTHETIC prices, no network, no LLM. Invented data only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant.accounts import routes as account_routes
from egoquant.accounts.routes import CONSENT_VERSION
from egoquant.advisor.context import open_context
from tests.accounts.test_accounts import A, B, DATA, consent, store, upload  # noqa: F401
from tests.system.test_risk_game_blend import expected_class, game_level, game_score, questionnaire_side

pytestmark = pytest.mark.skipif("game_played" not in egoquant_routes.ProfileOut.model_fields,
                                reason="the Risk Game fields are not in this tree yet (docs/RISK_GAME_BLEND.md)")

PLANNED = {"ticker": "VTI", "side": "buy", "amount": 1500}
OLD_ANSWER_KEYS = {"questionnaire_version", "choices", "multi_choices", "amount_invested", "amount_needed"}


def real_app() -> FastAPI:
    """Only the routers, but with main.app's 422 handler: it is what keeps the values the client sent out of an error body."""
    import main
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, main.validation_error)
    return app


@pytest.fixture
def app_client(store):                                                        # noqa: F811
    app = real_app()
    app.include_router(egoquant_routes.router)                                 # /account/portfolio, /account/trade-check, ...
    with patch.dict(os.environ, {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off"}):
        os.environ.pop("EGOQUANT_RISK_GAME_WEIGHT", None)
        os.environ.pop("EGOQUANT_RISK_GAME_MAY_RAISE", None)
        yield TestClient(app)


def stamp(minutes_ago: float = 5) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


CAUTIOUS = {"tolerance": 5, "loss_acceptance": 5, "crash_test": {"sold_at_drawdown": 0.1, "sold_fraction": 1.0, "max_drawdown_seen": 0.12}}


def cautious():
    return {**CAUTIOUS, "completed_at": stamp(5)}


def request(game: dict | None = None, **top):
    from tests.accounts.test_accounts import portfolio_request
    body = portfolio_request(**top)
    if game is not None:
        body["answers"] = {**body["answers"], "risk_game": game}
    return body


def build(client, body, headers=A):
    egoquant_routes._portfolio_timestamps.clear()
    return client.post("/account/portfolio", json=body, headers=headers)


def side(body):
    t, cap, gate, base = questionnaire_side({k: v for k, v in body["answers"].items() if k != "risk_game"})
    return t, cap, gate, base


def want_class(body, game) -> int:
    t, cap, gate, _ = side(body)
    level = None if game is None else game_level(game_score(game["tolerance"], game.get("loss_acceptance")))
    return expected_class(t, cap, gate, level)


def answers_dicts(node):
    """Every dict in a nested structure that looks like questionnaire answers (the top-level PortfolioIn.risk_game key of a sealed
    what-if request existed before the game and is not what is meant here)."""
    if isinstance(node, dict):
        if "choices" in node and "amount_invested" in node:
            yield node
        for value in node.values():
            yield from answers_dicts(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from answers_dicts(value)


def saved_row(store, rid, user="user-a"):
    return next(r for r in store.tables["reports"] if r["id"] == rid and r["user_id"] == user)


def test_the_persona_changes_class_with_a_cautious_game():
    """The premise of the tests below: a cautious game really lowers this investor's category."""
    body = request()
    assert want_class(body, cautious()) < want_class(body, None) == int(side(body)[3][1:])


# ---------------------------------------------------------------- /account/portfolio and the saved row
def test_a_report_built_without_a_game_keeps_exactly_the_old_saved_answers(app_client, store):
    body = request()
    out = build(app_client, body)
    assert out.status_code == 200, out.text
    answers = saved_row(store, out.json()["report_id"])["report"]["inputs"]["answers"]
    assert set(answers) == OLD_ANSWER_KEYS                                     # the same keys as before the game existed
    assert not [a for a in answers_dicts(store.tables) if "risk_game" in a]    # no answers anywhere (saved row, sealed inputs) carry the key
    row = app_client.get(f"/account/reports/{out.json()['report_id']}", headers=A)
    assert row.status_code == 200 and not [a for a in answers_dicts(row.json()) if "risk_game" in a]
    assert out.json()["c_level"] == f"C{want_class(body, None)}"


def test_a_report_built_with_a_game_keeps_the_game_and_the_blended_category_everywhere(app_client, store):
    game = cautious()
    body = request(game)
    out = build(app_client, body)
    assert out.status_code == 200, out.text
    data, want = out.json(), f"C{want_class(body, game)}"
    assert want != f"C{want_class(body, None)}"
    rid = data["report_id"]
    # the saved row: the game is in inputs.answers, as sent; the category everywhere is the blended one
    row = saved_row(store, rid)
    assert set(row["report"]["inputs"]["answers"]) == OLD_ANSWER_KEYS | {"risk_game"}
    saved_game = row["report"]["inputs"]["answers"]["risk_game"]
    assert saved_game["tolerance"] == 5 and saved_game["loss_acceptance"] == 5
    assert saved_game["crash_test"] == {"sold_at_drawdown": 0.1, "sold_fraction": 1.0, "max_drawdown_seen": 0.12}
    assert row["report"]["c_level"] == want == data["c_level"] == data["report"]["risk_class"]
    # the saved row survives GET, for its owner only
    got = app_client.get(f"/account/reports/{rid}", headers=A)
    assert got.status_code == 200 and got.json()["report"]["inputs"]["answers"]["risk_game"] == saved_game
    assert got.json()["report"]["c_level"] == want
    assert [r["id"] for r in app_client.get("/account/reports", headers=A).json()["reports"]] == [rid]
    assert app_client.get(f"/account/reports/{rid}", headers=B).status_code == 404
    assert app_client.get(f"/account/reports/{rid}/contexts", headers=B).status_code == 404
    # the sealed contexts of the build carry the blended category
    assert open_context(data["chat_context"])["c_level"] == want
    what_if = open_context(data["what_if_context"])
    assert what_if["c_level"] == want and what_if["max_r_level"] == int(want[1:]) and what_if["report_id"] == rid
    assert all(level <= what_if["max_r_level"] for level in what_if["r_levels"].values())
    # and so do the contexts re-sealed from the saved row later (the Advisor on a saved report)
    again = app_client.get(f"/account/reports/{rid}/contexts", headers=A).json()
    assert open_context(again["chat_context"])["c_level"] == want
    assert again["answers"]["risk_game"] == saved_game and again["expired"] is False


def test_a_game_replayed_later_changes_new_builds_only(app_client, store):
    first_body, second_body = request(cautious()), request()
    first = build(app_client, first_body).json()
    second = build(app_client, second_body).json()                             # the same investor, no game this time
    assert first["c_level"] == f"C{want_class(first_body, cautious())}" and second["c_level"] == f"C{want_class(second_body, None)}"
    assert first["c_level"] != second["c_level"]
    assert saved_row(store, first["report_id"])["report"]["c_level"] == first["c_level"]     # the old row still says what it said
    assert "risk_game" in saved_row(store, first["report_id"])["report"]["inputs"]["answers"]
    assert "risk_game" not in saved_row(store, second["report_id"])["report"]["inputs"]["answers"]
    assert open_context(first["chat_context"])["c_level"] == first["c_level"]                # the old seals open and say the old class
    assert open_context(second["chat_context"])["c_level"] == second["c_level"]
    assert first["chat_context"] != second["chat_context"]
    third = build(app_client, request(cautious())).json()                      # the game played again: the first class returns
    assert third["c_level"] == first["c_level"] and third["report_id"] not in (first["report_id"], second["report_id"])
    assert len(app_client.get("/account/reports", headers=A).json()["reports"]) == 3


def test_a_game_in_two_places_or_a_hostile_game_saves_nothing_and_echoes_nothing(app_client, store):
    inside, outside = cautious(), {"tolerance": 100, "loss_acceptance": 100, "completed_at": stamp(5)}
    different = build(app_client, {**request(inside), "risk_game": outside})
    assert different.status_code == 422 and '"input"' not in different.text and '"ctx"' not in different.text
    for hostile in ({"tolerance": 101, "completed_at": stamp()}, {"tolerance": "ZZ-SENTINEL-1", "completed_at": stamp()},
                    {"tolerance": 50, "completed_at": stamp(-60 * 24)}, {"tolerance": 50, "completed_at": "ZZ-SENTINEL-2"},
                    {"tolerance": 50, "completed_at": stamp(), "ZZ-SENTINEL-3": 1}):
        r = build(app_client, request(hostile))
        assert r.status_code == 422 and "ZZ-SENTINEL" not in r.text, hostile
    assert not store.tables.get("reports")                                     # nothing was saved by any of them
    agreeing = build(app_client, {**request(inside), "risk_game": dict(inside)})
    assert agreeing.status_code == 200 and len(store.tables["reports"]) == 1   # equal values are fine


def test_delete_my_data_erases_the_saved_game_with_the_rest(app_client, store):
    out = build(app_client, request(cautious()))
    assert out.status_code == 200 and "risk_game" in store.everything()
    other = build(app_client, request(cautious()), headers=B)
    assert app_client.delete("/account/data", headers=A).status_code == 200
    assert not [r for r in store.tables.get("reports", []) if r["user_id"] == "user-a"]
    assert [r for r in store.tables["reports"] if r["user_id"] == "user-b"] and other.status_code == 200   # B's data is not touched
    assert app_client.get(f"/account/reports/{out.json()['report_id']}", headers=A).status_code == 404


# ---------------------------------------------------------------- /account/trade-check
def agree(client):
    consent(client, A)
    assert client.post("/account/consent", json={"scope": "trade_check", "version": CONSENT_VERSION}, headers=A).status_code == 200
    assert upload(client, A, "ibkr_steady_holder.csv").status_code == 200


def test_the_account_trade_check_uses_the_blended_category(app_client, store):
    agree(app_client)
    game = cautious()
    plain, played = request(), request(game)
    for body, klass in ((plain, want_class(plain, None)), (played, want_class(played, game))):
        egoquant_routes._portfolio_timestamps.clear()
        r = app_client.post("/account/trade-check", json={**body, "planned": PLANNED}, headers=A)
        assert r.status_code == 200, r.text[:300]
        check = r.json()["check"]
        assert check["investor_max_r_level"] == klass
        assert open_context(r.json()["trade_context"])["investor_max_r_level"] == klass         # the sealed trade facts too
        saved = [row for row in store.tables["trade_checks"] if row["id"] == r.json()["check_id"]][0]
        assert saved["result"]["investor_max_r_level"] == klass                                   # and the saved check
    assert want_class(plain, None) != want_class(played, game)
    differing = {**played, "risk_game": {"tolerance": 100, "completed_at": stamp()}, "planned": PLANNED}
    egoquant_routes._portfolio_timestamps.clear()
    refused = app_client.post("/account/trade-check", json=differing, headers=A)
    assert refused.status_code == 422 and len(store.tables["trade_checks"]) == 2               # nothing saved for the refused one


# ---------------------------------------------------------------- the on-chain record
def test_the_record_is_unchanged_without_a_game_and_carries_the_game_with_one(app_client, store):
    from egoquant.anchoring.canonical import canonical_text
    from egoquant.anchoring.record import build_record

    def record_for(body):
        rid = build(app_client, body).json()["report_id"]
        saved = app_client.get(f"/account/reports/{rid}", headers=A).json()["report"]
        log = [{"at": "2026-10-20T08:00:00+00:00", "action": "report_generated", "details": {"report_id": rid}}]
        return build_record(saved, report_id=rid, log_rows=log, consent_version="2026-10-20",
                            confirmed_at=datetime(2026, 10, 20, 8, 5, tzinfo=timezone.utc)), body

    plain, plain_body = record_for(request())
    assert set(plain["investor"]["answers"]) == {"choices", "multi_choices", "amount_invested_cents", "amount_needed_cents"}
    assert "risk_game" not in canonical_text(plain)                            # byte-identical shape to a report without a game
    assert plain["investor"]["risk_class"] == f"C{want_class(plain_body, None)}"

    game = cautious()
    played, played_body = record_for(request(game))
    assert played["investor"]["risk_class"] == f"C{want_class(played_body, game)}" != plain["investor"]["risk_class"]
    assert played["investor"]["answers"]["risk_game"] == {
        "tolerance": 5, "loss_acceptance": 5,
        "crash_test": {"max_drawdown_seen_ppm": 120_000, "sold_fraction_ppm": 1_000_000, "sold_at_drawdown_ppm": 100_000}}
    assert {k: v for k, v in played["investor"]["answers"].items() if k != "risk_game"} == plain["investor"]["answers"]
    assert "completed_at" not in canonical_text(played)                        # when it was played is not an input
    assert canonical_text(played) != canonical_text(plain)                     # so the fingerprint covers the game


@pytest.fixture
def seal_api(store, app_client):                                              # noqa: F811
    pytest.importorskip("eth_account")
    from egoquant.anchoring import routes as anchoring_routes
    from egoquant.anchoring.client import FakeAnchorClient
    from tests.anchoring.test_routes import SealStore
    fake_store = SealStore()                                                   # FakeStore + migration 006's columns
    from egoquant.accounts.store import set_store
    set_store(fake_store)
    chain = FakeAnchorClient()
    anchoring_routes.set_anchor_client(chain)
    app = real_app()
    app.include_router(account_routes.router)
    app.include_router(egoquant_routes.router)
    app.include_router(anchoring_routes.router)
    with patch.dict(os.environ, {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off", "EGOQUANT_NEWS_WORKER": "off"}):
        yield TestClient(app), fake_store, chain
    anchoring_routes.set_anchor_client()
    set_store(None)


@pytest.mark.parametrize("with_game", [False, True])
def test_a_plan_with_or_without_a_game_confirms_seals_and_verifies(seal_api, with_game):
    api, fake_store, chain = seal_api
    game = cautious() if with_game else None
    body = request(game)
    egoquant_routes._portfolio_timestamps.clear()
    built = api.post("/account/portfolio", json=body, headers=A)
    assert built.status_code == 200, built.text[:200]
    rid = built.json()["report_id"]
    if not built.json()["approved"] or built.json()["violations"]:
        pytest.skip("this persona's synthetic portfolio was not approved; nothing to seal")
    version = api.get("/anchoring/status").json()["consent_version"]               # the current seal consent text
    confirmed = api.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A)
    assert confirmed.status_code == 200, confirmed.text[:300]
    chain.mine()
    proof = api.get(f"/account/reports/{rid}/proof", headers=A).json()
    record = json.loads(proof["record"])
    assert record["investor"]["risk_class"] == f"C{want_class(body, game)}"
    answers = record["investor"]["answers"]
    assert ("risk_game" in answers) is with_game
    assert set(answers) == {"choices", "multi_choices", "amount_invested_cents", "amount_needed_cents"} | ({"risk_game"} if with_game else set())
    assert "completed_at" not in proof["record"]
    verified = api.post("/verify", json=proof).json()
    assert verified["match"] is True and False not in verified["checks"].values()     # v2: the document is checked,
    assert verified["checks"]["document"] is True                                      # the report JSON is not in the file
    tampered = json.loads(proof["record"])
    if with_game:
        tampered["investor"]["answers"]["risk_game"]["tolerance"] = 95            # a bolder game after the fact
    else:
        tampered["investor"]["answers"]["risk_game"] = {"tolerance": 95}           # a game added after the fact
    from egoquant.anchoring.canonical import canonical_text
    assert api.post("/verify", json={**proof, "record": canonical_text(tampered)}).json()["match"] is False
