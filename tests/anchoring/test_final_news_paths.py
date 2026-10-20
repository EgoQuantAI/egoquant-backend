"""Every way of getting a saved report gives the FINISHED news, and only a report with finished news is sealed.

Paths: build-and-save (/account/portfolio, then the briefing job, then POST .../briefing), open (GET the saved
report), download (GET .../download, with or without migration 004 in the database). Offline: synthetic prices,
sample news (EGOQUANT_BRIEFING=fixture), the fake account store, the fake chain."""

from __future__ import annotations

import json
import time
from html import escape

import pytest
from fastapi.testclient import TestClient

from egoquant.accounts import routes as account_routes
from egoquant.accounts.store import AccountError, set_store
from egoquant.anchoring import routes as anchoring
from egoquant.anchoring.canonical import report_sha256
from egoquant.anchoring.client import FakeAnchorClient
from egoquant.anchoring.html import INSTRUCTIONS, NOT_SEALED
from egoquant.briefing import final, jobs, web
from tests.accounts.test_accounts import A, TOKENS
from tests.anchoring.test_routes import SealStore

B = {"Authorization": "Bearer token-b"}


class Store(SealStore):
    """SealStore + migration 004's replace_report_briefing (or, with has_004=False, the function is missing)."""

    has_004 = True

    def rpc(self, token, name, args):
        if name != "replace_report_briefing":
            return super().rpc(token, name, args)
        if not self.has_004:
            raise AccountError(502, "The account service could not complete the request.")
        assert set(args["sections"]) <= set(final.AGENT_D_KEYS)
        rows = [r for r in self._rls(token, "reports") if r["id"] == args["rid"]]
        if not rows:
            raise AccountError(502, "The account service could not complete the request.")
        rows[0]["report"]["report"].update(args["sections"])
        return None


def _reset_rate_limits() -> None:
    """Many builds in one test process: start each test with empty per-minute counters, like the routes' own tests."""
    import egoquant_routes
    for name in dir(egoquant_routes):
        value = getattr(egoquant_routes, name)
        if name.endswith(("_timestamps", "_hits")) and hasattr(value, "clear"):
            value.clear()
    from egoquant.briefing import web as briefing_web
    for module in (jobs, briefing_web):
        for name in dir(module):
            value = getattr(module, name)
            if name.endswith(("_timestamps", "_hits", "_starts")) and hasattr(value, "clear"):
                value.clear()


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("EGOQUANT_DATA", "synthetic")
    monkeypatch.setenv("EGOQUANT_BRIEFING", "fixture")
    monkeypatch.setenv("EGOQUANT_CACHE_DIR", str(tmp_path))
    saved_tokens = dict(TOKENS)
    s = Store()
    set_store(s)
    account_routes._user_hits.clear()
    jobs.clear_jobs()
    web.clear_feed_cache()
    _reset_rate_limits()
    yield s
    set_store(None)
    jobs.clear_jobs()
    TOKENS.clear()
    TOKENS.update(saved_tokens)


@pytest.fixture
def chain():
    fake = FakeAnchorClient()
    anchoring.set_anchor_client(fake)
    yield fake
    anchoring.set_anchor_client()


@pytest.fixture
def client(store):
    from main import app
    return TestClient(app)


def build_and_save(client) -> dict:
    from egoquant.demo.personas import PERSONAS
    from tests.test_egoquant_routes import portfolio_body
    persona = next(p for p in PERSONAS.values() if p.expected_class == "C3")
    r = client.post("/account/portfolio", json=portfolio_body(persona), headers=A)
    assert r.status_code == 200, r.text
    return r.json()


def finished_job(client, out) -> str:
    job = client.post("/briefing/start", json={"briefing_context": out["briefing_context"]}).json()["job"]
    for _ in range(200):
        if client.get(f"/briefing/{job}").json()["state"] != "running":
            break
        time.sleep(0.05)
    assert client.get(f"/briefing/{job}").json()["state"] == "done"
    return job


def saved_report(store, report_id) -> dict:
    return next(r for r in store.tables["reports"] if r["id"] == report_id)["report"]


def test_the_job_marks_its_news_final_and_the_quick_version_is_not(client, store):
    out = build_and_save(client)
    assert not final.news_is_final(saved_report(store, out["report_id"])["report"])     # the in-request version
    job = finished_job(client, out)
    week = jobs.get_job(job).result["week"]
    assert week["metadata"]["final_job"] == job
    assert final.news_is_final({"market_briefing": week}) and final.news_is_final({"market_briefing": None})


def test_build_and_save_then_finalize_then_seal_covers_the_final_report(client, store, chain):
    out = build_and_save(client)
    rid = out["report_id"]
    version = client.get("/anchoring/status").json()["consent_version"]
    early = client.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A)
    assert early.status_code == 409 and early.json()["detail"] == anchoring.NEWS_NOT_FINAL and chain.sent == []
    job = finished_job(client, out)
    assert client.post(f"/account/reports/{rid}/briefing", json={"job": job}, headers=A).status_code == 200
    opened = client.get(f"/account/reports/{rid}", headers=A).json()                     # the "open" path
    assert final.news_is_final(opened["report"]["report"])
    sealed = client.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A)
    assert sealed.status_code == 200 and sealed.json()["seal"]["status"] == "submitted"
    record = json.loads(store.tables["advisory_records"][0]["record_canonical"])
    assert record["versions"]["report_sha256"] == report_sha256(saved_report(store, rid))   # the FINAL saved report


def test_download_has_the_final_news_even_without_migration_004(client, store, monkeypatch):
    store.has_004 = False
    out = build_and_save(client)
    rid = out["report_id"]
    job = finished_job(client, out)
    assert client.post(f"/account/reports/{rid}/briefing", json={"job": job}, headers=A).status_code == 502
    rendered: list[dict] = []
    real = final.render_report_html
    monkeypatch.setattr(final, "render_report_html", lambda report: rendered.append(report) or real(report))
    page = client.get(f"/account/reports/{rid}/download", params={"job": job}, headers=A)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "attachment" in page.headers["content-disposition"]
    assert final.news_is_final(rendered[-1])                       # rendered from the finished job, not the saved copy
    assert rendered[-1]["allocation"] == out["report"]["allocation"]                    # the numbers are the saved ones
    client.get(f"/account/reports/{rid}/download", headers=A)                         # no job: the saved version
    assert not final.news_is_final(rendered[-1])


def test_download_ignores_a_job_of_another_report(client, store, monkeypatch):
    first = build_and_save(client)
    job = finished_job(client, first)
    second = build_and_save(client)
    row = saved_report(store, second["report_id"])
    row["report"]["allocation"][0]["weight"] += 0.01                 # other numbers: the digest does not match
    rendered: list[dict] = []
    real = final.render_report_html
    monkeypatch.setattr(final, "render_report_html", lambda report: rendered.append(report) or real(report))
    assert client.get(f"/account/reports/{second['report_id']}/download", params={"job": job}, headers=A).status_code == 200
    assert not final.news_is_final(rendered[-1])


def test_download_seal_section_sealed_not_sealed_and_off(client, store, chain):
    out = build_and_save(client)
    rid = out["report_id"]
    page = client.get(f"/account/reports/{rid}/download", headers=A).text
    assert "<h2>Seal</h2>" in page and NOT_SEALED in page
    job = finished_job(client, out)
    client.post(f"/account/reports/{rid}/briefing", json={"job": job}, headers=A)
    version = client.get("/anchoring/status").json()["consent_version"]
    tx = client.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A).json()["seal"]["tx_hash"]
    chain.mine()
    client.get(f"/account/reports/{rid}/seal", headers=A)                              # records "confirmed"
    page = client.get(f"/account/reports/{rid}/download", headers=A).text
    commitment = store.tables["advisory_records"][0]["commitment_hex"]
    assert "Sealed on the Sepolia test network ✓" in page and tx in page and commitment in page
    assert f"https://sepolia.etherscan.io/tx/{tx}" in page and chain.address in page
    assert escape(INSTRUCTIONS) in page                       # "open EgoQuant → Verify a proof file → upload ..."
    assert page.index("<h2>Seal</h2>") > page.index("<!--EGOQ-SEAL-FOOTER-BEGIN-->")   # v2: in the footer
    anchoring.set_anchor_client(None)                 # sealing switched off later: a SEALED report is still the same file
    assert client.get(f"/account/reports/{rid}/download", headers=A).text == page


def test_download_is_the_owners_only(client, store):
    out = build_and_save(client)
    assert client.get(f"/account/reports/{out['report_id']}/download", headers=B).status_code == 404
    assert client.get(f"/account/reports/{out['report_id']}/download").status_code == 401
    assert client.get(f"/account/reports/{out['report_id']}/download", params={"job": "../x"}, headers=A).status_code == 422


def test_a_large_finished_briefing_keeps_its_week_view_and_the_download_shows_it_all(client, store, monkeypatch):
    import egoquant_routes
    out = build_and_save(client)
    rid = out["report_id"]
    job = finished_job(client, out)
    result = jobs.get_job(job).result
    week_only = len(json.dumps({**final.final_sections(result), "market_briefing_month": None}))
    monkeypatch.setattr(egoquant_routes, "_MAX_SAVED_REPORT_BYTES", week_only + 10)     # full > limit >= week only
    assert client.post(f"/account/reports/{rid}/briefing", json={"job": job}, headers=A).status_code == 200
    saved = saved_report(store, rid)["report"]
    assert saved["market_briefing"] is not None and saved["market_briefing_month"] is None
    assert final.news_is_final(saved)
    rendered: list[dict] = []
    real = final.render_report_html
    monkeypatch.setattr(final, "render_report_html", lambda report: rendered.append(report) or real(report))
    client.get(f"/account/reports/{rid}/download", params={"job": job}, headers=A)
    assert rendered[-1]["market_briefing_month"] is not None                          # the download: the full news
