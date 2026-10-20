"""Saving the final news briefing into a saved report, and the sealed case.

What the page can rely on (the states behind the Confirm button, see overnight/bartu.md "For Umut"):
  running   -> 409 "The news is still being rated (n of m)."          (not settled: Confirm stays off)
  failed    -> 409 "The news briefing could not be finished; the report keeps its first version."   (settled)
  done      -> 200 {"ok": true}                                       (settled: the report holds the final briefing)
  unknown   -> 404                                                    (settled: nothing will ever be saved)
  sealed    -> 409 REPORT_SEALED_MESSAGE, nothing written             (settled; the plan is already sealed)

The sealed case needs the seal router (Onur wires the route after it lands): that test is a strict xfail and turns
into an XPASS failure the day the route is wired, so the marker gets removed. The guard and the wording are in
egoquant/accounts/sealed.py. No network, no AI.
"""

import pytest
from fastapi import HTTPException

from egoquant.accounts import sealed
from egoquant.accounts.sealed import REPORT_SEALED_MESSAGE, require_unsealed
from egoquant.accounts.store import AccountError
from egoquant.briefing import jobs
from tests.accounts.test_accounts import A
from tests.briefing.test_final_report import (SavingStore, client, finished_job, portfolio,  # noqa: F401
                                              saving_store)

B = {"Authorization": "Bearer token-b"}


def save(client, report_id, job, headers=A):  # noqa: F811
    return client.post(f"/account/reports/{report_id}/briefing", json={"job": job}, headers=headers)


def seal(store, report_id, user_id="user-a"):
    """What the seal router leaves behind (docs/ANCHORING.md): one advisory_records row for the report."""
    store.tables.setdefault("advisory_records", []).append(
        {"id": "00000000-0000-4000-8000-0000000000aa", "user_id": user_id, "report_id": report_id, "record_version": 1,
         "schema": "egoq-record-1", "commitment_hex": "0" * 64})


# ---- the wording ---------------------------------------------------------------------------------------------
def test_the_sentence_is_plain():
    text = REPORT_SEALED_MESSAGE
    assert text.startswith("This plan is already confirmed and sealed")
    assert "can no longer change" in text and "exactly as you confirmed it" in text
    assert len(text) < 200 and text.count(".") == 2                         # two short sentences
    for jargon in ("error", "409", "409", "advisory", "record_seal", "commitment", "tx", "blockchain", "hash",
                   "database", "Traceback", "exception", "HTTP"):
        assert jargon.lower() not in text.lower(), jargon


def test_the_guard_answers_409_with_that_sentence_only_when_a_seal_exists():
    assert require_unsealed([]) is None and require_unsealed(None) is None
    with pytest.raises(HTTPException) as err:
        require_unsealed([{"id": "x"}])
    assert (err.value.status_code, err.value.detail) == (409, sealed.REPORT_SEALED_MESSAGE)


# ---- the states the Confirm button waits for (these pass today) -----------------------------------------------
def test_running_failed_done_and_unknown_jobs_give_distinct_answers(client, saving_store):  # noqa: F811
    out = portfolio(client, "/account/portfolio", headers=A)
    rid = out["report_id"]
    facts = jobs.open_briefing_context(out["briefing_context"])

    running = jobs.start_job(facts, runner=lambda *a, **k: None, synchronous=True)        # stays "running"
    running.rated, running.total = 12, 40
    r = save(client, rid, running.id)
    assert r.status_code == 409 and r.json()["detail"] == "The news is still being rated (12 of 40)."

    def fail(job, *args, **kwargs):
        job.state = "failed"

    failed = jobs.start_job(facts, runner=fail, synchronous=True)
    r = save(client, rid, failed.id)
    assert r.status_code == 409 and r.json()["detail"] == (
        "The news briefing could not be finished; the report keeps its first version.")

    assert save(client, rid, "no-such-job").status_code == 404

    done = finished_job(client, out)
    r = save(client, rid, done["job"])
    assert r.status_code == 200 and r.json() == {"ok": True, "report_id": rid}


def test_the_failed_job_leaves_the_saved_report_exactly_as_it_was(client, saving_store):  # noqa: F811
    out = portfolio(client, "/account/portfolio", headers=A)
    before = repr(saving_store.tables["reports"][0]["report"])
    facts = jobs.open_briefing_context(out["briefing_context"])
    failed = jobs.start_job(facts, runner=lambda job, *a, **k: setattr(job, "state", "failed"), synchronous=True)
    assert save(client, out["report_id"], failed.id).status_code == 409
    assert repr(saving_store.tables["reports"][0]["report"]) == before


def test_a_database_without_the_seal_tables_still_saves_the_briefing(client, saving_store):  # noqa: F811
    """Migration for the seal tables not applied yet: asking for them must never break the save."""
    real = saving_store.select

    def select(token, table, params):
        if table in ("advisory_records", "record_seals"):
            raise AccountError(404, "Request failed.")
        return real(token, table, params)

    saving_store.select = select
    out = portfolio(client, "/account/portfolio", headers=A)
    done = finished_job(client, out)
    assert save(client, out["report_id"], done["job"]).status_code == 200


# ---- the sealed report (route wired) --------------------------------------------------------------
def test_saving_a_briefing_into_a_sealed_report_is_a_plain_409_and_writes_nothing(client, saving_store):  # noqa: F811
    out = portfolio(client, "/account/portfolio", headers=A)
    done = finished_job(client, out)
    seal(saving_store, out["report_id"])
    before = repr(saving_store.tables["reports"][0]["report"])
    r = save(client, out["report_id"], done["job"])
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": REPORT_SEALED_MESSAGE}                      # the sentence, and nothing generic
    assert repr(saving_store.tables["reports"][0]["report"]) == before         # the sealed report did not change
    assert "Traceback" not in r.text and "advisory" not in r.text


def test_the_seal_check_runs_before_the_job_check(client, saving_store):  # noqa: F811
    """A sealed report says 'sealed' even when the job is still running or unknown: the reader needs the true reason."""
    out = portfolio(client, "/account/portfolio", headers=A)
    seal(saving_store, out["report_id"])
    for job in ("no-such-job",):
        r = save(client, out["report_id"], job)
        assert r.status_code == 409 and r.json()["detail"] == REPORT_SEALED_MESSAGE


def test_one_users_seal_is_invisible_to_another_user(client, saving_store):  # noqa: F811
    """A's seal row is A's (RLS). B asking to save into A's report gets "not found", never the sealed sentence."""
    a = portfolio(client, "/account/portfolio", headers=A)
    seal(saving_store, a["report_id"], user_id="user-a")
    done = finished_job(client, a)
    r = save(client, a["report_id"], done["job"], headers=B)
    assert r.status_code == 404 and r.json()["detail"] == "Report not found."
    assert REPORT_SEALED_MESSAGE not in r.text
