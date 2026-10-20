"""What /account/portfolio hands to on-chain sealing (egoquant/anchoring): the answers saved with the report and two log rows."""

import pytest

from egoquant.accounts import routes as account_routes
from egoquant.accounts.routes import CONSENT_VERSION, SEAL_CONSENT_VERSION, has_consent
from egoquant.accounts.store import AccountError
from tests.accounts.test_accounts import A, app_client, client, consent, portfolio_request, store, upload  # noqa: F401


def build(app_client):  # noqa: F811
    consent(app_client, A)
    upload(app_client, A)
    body = portfolio_request()
    out = app_client.post("/account/portfolio", json=body, headers=A)
    assert out.status_code == 200, out.text
    return body, out.json()


def test_the_answers_are_saved_with_the_report_and_nothing_else_about_the_questionnaire(app_client, store):  # noqa: F811
    body, data = build(app_client)
    saved = store.tables["reports"][0]["report"]
    answers = saved["inputs"]["answers"]
    assert set(saved["inputs"]) == {"answers"}
    assert answers["choices"] == body["answers"]["choices"]
    assert answers["multi_choices"] == body["answers"].get("multi_choices", {})
    assert answers["amount_invested"] == body["answers"]["amount_invested"]
    assert answers["amount_needed"] == body["answers"]["amount_needed"]
    assert isinstance(answers["questionnaire_version"], str) and answers["questionnaire_version"]
    assert not any("score" in key for key in answers)                      # the questionnaire score is never kept
    assert app_client.get(f"/account/reports/{data['report_id']}", headers=A).json()["report"]["inputs"]["answers"] == answers


def test_the_log_has_questionnaire_submitted_then_report_generated_for_the_same_report(app_client, store):  # noqa: F811
    _, data = build(app_client)
    rows = [r for r in store.tables["operation_log"] if r["action"] in ("questionnaire_submitted", "report_generated")]
    assert [r["action"] for r in rows] == ["questionnaire_submitted", "report_generated"]
    assert {r["details"]["report_id"] for r in rows} == {data["report_id"]}
    assert "answers" not in str(rows[0]["details"]) and "amount" not in str(rows[0]["details"])


def test_a_database_that_does_not_know_the_new_action_yet_does_not_break_the_portfolio(app_client, store):  # noqa: F811
    real = store.insert

    def insert(token, table, rows):                                           # migration 006 not applied: the CHECK refuses
        if table == "operation_log" and any(r.get("action") == "questionnaire_submitted" for r in rows):
            raise AccountError(400, "Request failed.")
        return real(token, table, rows)

    store.insert = insert
    _, data = build(app_client)
    actions = [r["action"] for r in store.tables["operation_log"]]
    assert data["report_id"] and "report_generated" in actions and "questionnaire_submitted" not in actions


def test_the_seal_consent_has_its_own_version_and_does_not_touch_the_others(app_client, store):  # noqa: F811
    assert SEAL_CONSENT_VERSION != CONSENT_VERSION
    consent(app_client, A)
    user = type("U", (), {"id": "user-a"})()
    token = A["Authorization"].split()[-1]
    assert has_consent(token, user, "ibkr_data")                              # agreed to the current IBKR text
    assert not has_consent(token, user, "record_seal", SEAL_CONSENT_VERSION)  # but never to sealing
