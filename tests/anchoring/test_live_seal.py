"""LIVE smoke test (`pytest -m live tests/anchoring/test_live_seal.py`), Bartu's Mac only: one real Sepolia seal
of a FAKE demo record, then verify. Skipped without EGOQ_SEAL_PRIVATE_KEY. CI never runs it (-m "not live")."""

import os

import pytest

from egoquant.anchoring.canonical import canonical_text
from egoquant.anchoring.client import anchor_client_from_env, wait_for_block
from egoquant.anchoring.commit import commitment, new_salt
from egoquant.anchoring.proof import make_proof, verify_proof

pytestmark = pytest.mark.live


def test_one_real_seal():
    from dotenv import load_dotenv
    load_dotenv()
    if not os.getenv("EGOQ_SEAL_PRIVATE_KEY"):
        pytest.skip("EGOQ_SEAL_PRIVATE_KEY is not set")
    client = anchor_client_from_env(require_enabled=False)
    assert client is not None
    text = canonical_text({"schema": "egoq-record-1", "demo": "live smoke test, fake data"})
    salt = new_salt()
    c = commitment(salt, text.encode())
    r = client.anchor(c)
    assert r.status == "submitted", r.error_kind
    final = wait_for_block(client, r.tx_hash, timeout_s=180)
    assert final.status == "confirmed"
    proof = make_proof(record_text=text, salt=salt, commit=c, chain_id=client.chain_id, tx_hash=r.tx_hash,
                       sealing_address=client.address)
    assert verify_proof(proof, client.read_tx, [client.address])["match"] is True
