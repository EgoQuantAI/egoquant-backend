"""Proof files, verification (match + every mismatch kind) and the CLI, on the fake chain."""

import copy
import json

import pytest

from egoquant.anchoring import __main__ as cli
from egoquant.anchoring.canonical import canonical_text
from egoquant.anchoring.client import FakeAnchorClient
from egoquant.anchoring.commit import commitment, tx_data
from egoquant.anchoring.proof import ProofError, make_proof, verify_proof
from tests.anchoring.test_record import build, SAVED


@pytest.fixture
def chain():
    return FakeAnchorClient()


def sealed(chain, record=None, report=SAVED, salt=b"\x07" * 32):
    text = canonical_text(record or build())
    c = commitment(salt, text.encode())
    r = chain.anchor(c)
    chain.mine()
    return make_proof(record_text=text, salt=salt, commit=c, chain_id=chain.chain_id, tx_hash=r.tx_hash,
                      sealing_address=chain.address, report=report)


def verify(chain, proof, addresses=None):
    return verify_proof(proof, chain.read_tx, addresses or [chain.address])


def test_match(chain):
    out = verify(chain, sealed(chain))
    assert out["match"] is True and out["failed"] is None and out["block_number"]
    assert all(v is True for k, v in out["checks"].items() if k != "document") and out["checks"]["document"] is None   # v1: no document


def _tamper_record(proof, fn):
    record = json.loads(proof["record"])
    fn(record)
    return {**proof, "record": canonical_text(record)}


@pytest.mark.parametrize("fn", [
    lambda r: (r["allocation"][0].__setitem__("weight_ppm", r["allocation"][0]["weight_ppm"] + 1),
               r["allocation"][1].__setitem__("weight_ppm", r["allocation"][1]["weight_ppm"] - 1)),
    lambda r: r["investor"]["answers"]["choices"].__setitem__("G1", "a"),
    lambda r: r["operation_log"][0].__setitem__("at", "2026-10-20T08:10:10Z"),
])
def test_tampered_record_says_hash_differs(chain, fn):
    out = verify(chain, _tamper_record(sealed(chain), fn))
    assert out["match"] is False and out["failed"] == "hash differs"


def test_tampered_report_says_report_differs(chain):
    proof = sealed(chain)
    report = copy.deepcopy(proof["report"])
    report["report"]["allocation"][0]["weight"] = 0.41
    out = verify(chain, {**proof, "report": report})
    assert out["failed"] == "report differs" and out["checks"]["hash"] is True


def test_tx_not_found(chain):
    proof = sealed(chain)
    out = verify(chain, {**proof, "tx_hash": "0x" + "ab" * 32})
    assert out["failed"] == "tx not found" and out["match"] is False


def test_wrong_sender(chain):
    proof = sealed(chain)
    out = verify(chain, proof, addresses=["0x1111111111111111111111111111111111111111"])
    assert out["failed"] == "not sent by EgoQuant"


def test_wrong_prefix_and_wrong_chain(chain):
    proof = sealed(chain)
    c = bytes.fromhex(proof["commitment"])
    h1, h2 = "0x" + "01" * 32, "0x" + "02" * 32
    chain.add_tx(h1, sender=chain.address, to=chain.address, data=b"EGOQ2" + c)
    chain.add_tx(h2, sender=chain.address, to=chain.address, data=tx_data(c), chain_id=1)
    assert verify(chain, {**proof, "tx_hash": h1})["failed"] == "wrong prefix"
    assert verify(chain, {**proof, "tx_hash": h2})["failed"] == "wrong chain"


def test_not_yet_in_a_block(chain):
    text = canonical_text(build())
    c = commitment(b"\x01" * 32, text.encode())
    r = chain.anchor(c)
    proof = make_proof(record_text=text, salt=b"\x01" * 32, commit=c, chain_id=chain.chain_id, tx_hash=r.tx_hash,
                       sealing_address=chain.address)
    out = verify(chain, proof)
    assert out["failed"] == "not yet in a block" and out["checks"]["report"] is None


@pytest.mark.parametrize("bad", [None, [], {"proof": "x"}, {"proof": "egoq-proof-1", "record": "{}"},
                                 {"proof": "egoq-proof-1", "record": "{}", "salt": "00" * 32,
                                  "commitment": "00" * 32, "tx_hash": "nope"}])
def test_not_a_proof(chain, bad):
    with pytest.raises(ProofError):
        verify(chain, bad)


def test_cli_demo_seal_verify_tamper(tmp_path, monkeypatch, capsys, chain):
    monkeypatch.setattr(cli, "_client", lambda fake: chain)
    monkeypatch.setattr(cli, "_load_env", lambda path: None)
    rec = tmp_path / "demo.json"
    assert cli.main(["demo-record", str(rec)]) == 0
    assert cli.main(["seal", str(rec), "--report", str(rec.with_suffix(".report.json")), "--wait", "0"]) == 0
    proof_path = rec.with_suffix(".proof.json")
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["tx_hash"] and proof["report"]["c_level"] == "C3"
    chain.mine()
    assert cli.main(["verify", str(proof_path)]) == 0
    assert "Unchanged ✓" in capsys.readouterr().out
    bad = tmp_path / "tampered.proof.json"
    assert cli.main(["tamper", str(proof_path), str(bad)]) == 0
    assert cli.main(["verify", str(bad)]) == 1
    assert "Does not match ✗ (hash differs)" in capsys.readouterr().out


def test_cli_rpc_down_then_retry_uses_the_same_commitment(tmp_path, monkeypatch, chain):
    monkeypatch.setattr(cli, "_client", lambda fake: chain)
    monkeypatch.setattr(cli, "_load_env", lambda path: None)
    rec = tmp_path / "demo.json"
    cli.main(["demo-record", str(rec)])
    chain.down = True
    assert cli.main(["seal", str(rec), "--wait", "0"]) == 2
    proof = json.loads(rec.with_suffix(".proof.json").read_text(encoding="utf-8"))
    assert proof["tx_hash"] is None
    chain.down = False
    assert cli.main(["retry", str(tmp_path)]) == 0
    again = json.loads(rec.with_suffix(".proof.json").read_text(encoding="utf-8"))
    assert again["tx_hash"] and again["commitment"] == proof["commitment"] and again["salt"] == proof["salt"]
    assert chain.sent == [tx_data(bytes.fromhex(proof["commitment"]))]
    assert cli.main(["retry", str(tmp_path)]) == 0                      # already sent: nothing new
    assert len(chain.sent) == 1


def test_only_prefix_and_commitment_go_on_chain(chain):
    proof = sealed(chain)
    assert chain.sent == [b"EGOQ1" + bytes.fromhex(proof["commitment"])]


@pytest.mark.parametrize("weights", [[1.0], [0.6, 0.4], [0.5, 0.3, 0.2]])
def test_cli_tamper_always_changes_the_record(tmp_path, monkeypatch, chain, weights):
    """A tampered copy must never verify, also for a single-holding portfolio (100% in one fund)."""
    saved = copy.deepcopy(SAVED)
    saved["report"]["allocation"] = [{"asset_id": f"T{i}", "weight": w} for i, w in enumerate(weights)]
    proof = sealed(chain, record=build(saved), report=saved)
    path, bad = tmp_path / "p.proof.json", tmp_path / "bad.proof.json"
    path.write_text(json.dumps(proof))
    monkeypatch.setattr(cli, "_load_env", lambda p: None)
    assert cli.main(["tamper", str(path), str(bad)]) == 0
    tampered = json.loads(bad.read_text(encoding="utf-8"))
    assert tampered["record"] != proof["record"]
    assert verify(chain, tampered)["failed"] == "hash differs"
