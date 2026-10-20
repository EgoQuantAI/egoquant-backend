"""The anchoring router is wired into the real app (main.py), /verify has the 4.5 MB body limit (a proof file carries
the saved report), and with sealing not configured everything answers plainly (no 5xx surprise, nothing stored)."""

import json

import pytest
from fastapi.testclient import TestClient

from egoquant.anchoring import routes as anchoring
from main import _body_limit, app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def sealing_off():
    anchoring.set_anchor_client(None)
    yield
    anchoring.set_anchor_client()


def test_the_router_is_included():
    paths = set(app.openapi()["paths"])
    assert {"/anchoring/status", "/verify", "/verify/tx/{tx_hash}", "/account/reports/{report_id}/confirm",
            "/account/reports/{report_id}/seal", "/account/reports/{report_id}/proof",
            "/account/reports/{report_id}/viewed"} <= paths


@pytest.mark.parametrize("path", ["/verify", "/verify/tx/0x" + "ab" * 32])
def test_verify_is_in_the_4_5_mb_group(path):
    assert _body_limit(path) == 4_500_000


def test_a_2_mb_proof_reaches_verify_and_a_4_6_mb_one_is_refused_unread():
    proof = {"proof": "egoq-proof-1", "record": "{}", "salt": "00" * 32, "commitment": "00" * 32, "tx_hash": None,
             "chain_id": 11155111, "sealing_address": "0x" + "0" * 40, "report": {"pad": "A" * 2_000_000}}
    body = json.dumps(proof).encode()
    assert 2_000_000 < len(body) < 4_500_000
    out = client.post("/verify", content=body, headers={"Content-Type": "application/json"})
    assert out.status_code == 503 and out.json()["detail"] == anchoring.NOT_AVAILABLE    # reached the route
    big = client.post("/verify", content=b"A" * 4_600_000, headers={"Content-Type": "application/json"})
    assert big.status_code == 413 and big.json() == {"detail": "Request is too large."}


def test_off_means_hidden_not_broken():
    assert client.get("/anchoring/status").json() == {"enabled": False}
    assert client.get("/verify/tx/0x" + "ab" * 32).status_code == 503
    assert client.get("/verify/tx/not-a-hash").status_code == 422
