"""Seal v2: the sealed document, the downloadable sealed report and the technical seal file.

Attack A (a genuine proof + an edited report) and attack B (someone else's genuine report) must fail or be visible:
any change in the document part is caught, the username sits inside the fingerprint, and a forged record is caught
by the chain check. v1 seals still verify (marked as the older format). Real engine reports (synthetic prices, sample
news), the fake account store, the fake chain; no network."""

from __future__ import annotations

import json

import pytest

from egoquant.anchoring.canonical import canonical_text
from egoquant.anchoring.commit import commitment, new_salt
from egoquant.anchoring.proof import verify_proof, verify_sealed_report
from egoquant.anchoring.sealed import FOOTER_BEGIN, split_sealed_report
from tests.accounts.test_accounts import A
from tests.anchoring.test_final_news_paths import (build_and_save, chain, client, finished_job,  # noqa: F401
                                                   saved_report, store)


@pytest.fixture
def sealed(client, store, chain):  # noqa: F811
    """One real report: built, news finished, confirmed, mined. Returns (report id, sealed report, seal file)."""
    out = build_and_save(client)
    rid = out["report_id"]
    client.post(f"/account/reports/{rid}/briefing", json={"job": finished_job(client, out)}, headers=A)
    version = client.get("/anchoring/status").json()["consent_version"]
    assert client.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A).status_code == 200
    chain.mine()
    client.get(f"/account/reports/{rid}/seal", headers=A)
    page = client.get(f"/account/reports/{rid}/download", headers=A).text
    seal_file = client.get(f"/account/reports/{rid}/proof", headers=A).json()
    return rid, page, seal_file


def check_page(chain, page):  # noqa: F811
    return verify_sealed_report(page, chain.read_tx, [chain.address], explorer=chain.explorer)


def check_file(chain, seal_file):  # noqa: F811
    return verify_proof(seal_file, chain.read_tx, [chain.address], explorer=chain.explorer)


def username_of(store):  # noqa: F811
    return store.tables["profiles"][0]["username"]


def test_the_untouched_sealed_report_and_seal_file_verify(sealed, chain, store):  # noqa: F811
    rid, page, seal_file = sealed
    for out in (check_page(chain, page), check_file(chain, seal_file)):
        assert out["match"] is True and out["failed"] is None and out["format"] == "v2"
        assert out["checks"]["document"] is True and out["older_format_note"] is None
        assert out["sealed_for"] == username_of(store) and out["block_number"]
    record = json.loads(seal_file["record"])
    assert record["schema"] == "egoq-record-2" and record["client"] == {"username": username_of(store), "report_id": rid}
    assert set(record["client"]) == {"username", "report_id"}                  # never an email or a user id


def test_the_sealed_for_line_is_inside_the_fingerprinted_document(sealed, store):  # noqa: F811
    rid, page, seal_file = sealed
    document, _ = split_sealed_report(page)
    assert f"Sealed for: {username_of(store)} · report {rid[:8]}" in document
    assert document == seal_file["document"]


@pytest.mark.parametrize("where", [0.02, 0.3, 0.5, 0.8, 0.999])
def test_one_changed_character_anywhere_in_the_document_is_caught(sealed, chain, where):  # noqa: F811
    _, page, _ = sealed
    document, _ = split_sealed_report(page)
    i = int(len(document) * where)
    i += next(k for k in range(200) if document[i + k].isalnum())      # change a visible letter or digit
    changed = document[:i] + ("1" if document[i] != "1" else "2") + document[i + 1:] + page[len(document):]
    out = check_page(chain, changed)
    assert out["match"] is False and out["failed"] == "report changed" and out["checks"]["document"] is False


def test_changing_a_visible_weight_is_caught(sealed, chain):  # noqa: F811
    _, page, _ = sealed
    document, _ = split_sealed_report(page)
    import re
    m = re.search(r"\d+\.\d%", document)
    assert m, "the report shows weights"
    out = check_page(chain, page.replace(m.group(0), "99.9%", 1))
    assert out["failed"] == "report changed"


def test_changing_the_username_in_a_copy_breaks_the_fingerprint(sealed, chain, store):  # noqa: F811
    _, page, seal_file = sealed
    forged = page.replace(f"Sealed for: {username_of(store)}", "Sealed for: someone_else", 1)
    assert check_page(chain, forged)["failed"] == "report changed"
    forged_file = {**seal_file, "document": seal_file["document"].replace(username_of(store), "someone_else", 1)}
    assert check_file(chain, forged_file)["failed"] == "report changed"


def test_a_seal_file_with_a_changed_document_is_caught(sealed, chain):  # noqa: F811
    _, _, seal_file = sealed
    out = check_file(chain, {**seal_file, "document": seal_file["document"] + " "})
    assert out["match"] is False and out["failed"] == "report changed"


def test_a_forged_footer_record_is_caught_by_the_chain(sealed, chain):  # noqa: F811
    """Attack: an edited document + a NEW record whose document fingerprint matches it + a new salt and commitment.
    Everything inside the file agrees with itself; only the chain can say no."""
    _, page, _ = sealed
    document, fields = split_sealed_report(page)
    edited = document.replace("Approved", "Approved!", 1)
    record = json.loads(fields["record"])
    import hashlib
    record["versions"]["report_document_sha256"] = hashlib.sha256(edited.encode("utf-8")).hexdigest()
    text = canonical_text(record)
    salt = new_salt()
    forged_fields = {**fields, "record": text, "salt": salt.hex(), "commitment": commitment(salt, text.encode()).hex()}
    from egoquant.anchoring.sealed import sealed_report_html
    forged = sealed_report_html(edited, forged_fields, "<section><h2>Seal</h2></section>")
    out = check_page(chain, forged)
    assert out["checks"]["document"] is True and out["checks"]["hash"] is True       # it agrees with itself...
    assert out["match"] is False and out["checks"]["commitment"] is False           # ...but not with the chain


def test_two_downloads_of_one_sealed_report_are_byte_identical(sealed, client):  # noqa: F811
    rid, page, _ = sealed
    assert client.get(f"/account/reports/{rid}/download", headers=A).content == page.encode("utf-8")


def test_an_unsealed_report_says_never_sealed(client, store, chain):  # noqa: F811
    out = build_and_save(client)
    page = client.get(f"/account/reports/{out['report_id']}/download", headers=A).text
    assert FOOTER_BEGIN not in page
    result = check_page(chain, page)
    assert result["sealed"] is False and result["failed"] == "never sealed"


def test_v1_seals_still_verify_as_the_older_format(chain):  # noqa: F811
    from egoquant.anchoring.proof import make_proof
    text = canonical_text({"schema": "egoq-record-1", "allocation": [{"ticker": "BIL", "weight_ppm": 1_000_000}]})
    salt = new_salt()
    c = commitment(salt, text.encode())
    r = chain.anchor(c)
    chain.mine()
    proof = make_proof(record_text=text, salt=salt, commit=c, chain_id=chain.chain_id, tx_hash=r.tx_hash,
                       sealing_address=chain.address)
    out = check_file(chain, proof)
    assert out["match"] is True and out["format"] == "v1"
    assert out["older_format_note"] == "older seal format, no document fingerprint" and out["checks"]["document"] is None


def test_my_seals_lists_the_users_own_seals_only(sealed, client, chain):  # noqa: F811
    rid, _, seal_file = sealed
    mine = client.get("/account/seals", headers=A).json()["seals"]
    assert [s["report_id"] for s in mine] == [rid] and mine[0]["tx_hash"] == seal_file["tx_hash"]
    assert mine[0]["status"] == "confirmed" and mine[0]["block_number"]
    assert client.get("/account/seals", headers={"Authorization": "Bearer token-b"}).json()["seals"] == []


def test_without_migration_008_the_plan_is_still_confirmed_and_sealed_as_v1(client, store, chain):  # noqa: F811
    """Before 008 the database has no sealed_document column: PostgREST refuses the row (the store answers 502). The
    plan is never blocked: it is sealed in the v1 format, and its seal file verifies as the older format."""
    from egoquant.accounts.store import AccountError
    real_insert = store.insert

    def no_column(token, table, rows):
        if table == "advisory_records" and any("sealed_document" in r for r in rows):
            raise AccountError(502, "The account service could not complete the request.")
        return real_insert(token, table, rows)

    store.insert = no_column
    out = build_and_save(client)
    rid = out["report_id"]
    client.post(f"/account/reports/{rid}/briefing", json={"job": finished_job(client, out)}, headers=A)
    version = client.get("/anchoring/status").json()["consent_version"]
    confirmed = client.post(f"/account/reports/{rid}/confirm", json={"consent_version": version}, headers=A)
    assert confirmed.status_code == 200 and confirmed.json()["confirmed"] is True
    chain.mine()
    seal_file = client.get(f"/account/reports/{rid}/proof", headers=A).json()
    assert json.loads(seal_file["record"])["schema"] == "egoq-record-1" and "document" not in seal_file
    result = check_file(chain, seal_file)
    assert result["match"] is True and result["format"] == "v1"
