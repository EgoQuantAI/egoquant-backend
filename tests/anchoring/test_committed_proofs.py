"""Every proof file committed as evidence still verifies with the final code, and every tampered copy still fails.

The chain side comes from `sepolia_snapshot.json`: a read-only snapshot (public RPC, 20 Oct 2026) of the real Sepolia
transactions those proofs name (sender, input `EGOQ1` + commitment, block, status). No network, no key. The v1 proofs in
`docs/anchoring/` and the v2 demo sealed report (`demo-sealed-report.v2.html`, the same bytes as the frontend's
`public/demo-sealed-report.html`) are byte-exact files: a CR from a Windows checkout would break them, so that fails too."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from egoquant.anchoring.client import TxInfo
from egoquant.anchoring.proof import verify_proof, verify_sealed_report

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "anchoring"
SNAPSHOT = json.loads((Path(__file__).with_name("sepolia_snapshot.json")).read_text(encoding="utf-8"))
SEALING_ADDRESS = "0x3c9E28032A834F958BaBE168aa9AcC116D2c9ddC"
PROOFS = sorted(p for p in EVIDENCE.glob("*.proof.json") if ".tampered." not in p.name)
TAMPERED = sorted(EVIDENCE.glob("*.tampered.proof.json"))
DEMO_V2 = EVIDENCE / "demo-sealed-report.v2.html"


def read_tx(tx_hash: str) -> TxInfo | None:
    t = SNAPSHOT.get(tx_hash)
    if t is None:
        return None
    when = datetime.fromtimestamp(int(t["timestamp"], 16), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return TxInfo(tx_hash=tx_hash, chain_id=int(t["chainId"], 16), sender=t["from"], to=t["to"],
                  data=bytes.fromhex(t["input"][2:]), status="confirmed" if t["status"] == "0x1" else "failed",
                  block_number=int(t["blockNumber"], 16), block_time=when)


def test_the_evidence_folder_has_the_proofs_we_cite():
    assert len(PROOFS) >= 5 and len(TAMPERED) >= 5 and DEMO_V2.exists()


@pytest.mark.parametrize("path", PROOFS, ids=lambda p: p.name)
def test_every_committed_proof_still_verifies(path):
    out = verify_proof(json.loads(path.read_text(encoding="utf-8")), read_tx, [SEALING_ADDRESS])
    assert out["match"] is True and out["failed"] is None, out["failed"]
    assert out["format"] == "v1" and out["block_number"]


@pytest.mark.parametrize("path", TAMPERED, ids=lambda p: p.name)
def test_every_committed_tampered_copy_still_fails(path):
    out = verify_proof(json.loads(path.read_text(encoding="utf-8")), read_tx, [SEALING_ADDRESS])
    assert out["match"] is False and out["failed"]


def test_the_v2_demo_sealed_report_verifies_and_says_whom_it_was_sealed_for():
    page = DEMO_V2.read_bytes().decode("utf-8")
    out = verify_sealed_report(page, read_tx, [SEALING_ADDRESS])
    assert out["match"] is True and out["format"] == "v2" and out["sealed_for"] == "demo_investor"
    assert out["block_number"] == 11_951_549
    edited = page.replace("Sealed for: demo_investor", "Sealed for: someone_else", 1)
    assert verify_sealed_report(edited, read_tx, [SEALING_ADDRESS])["failed"] == "report changed"


@pytest.mark.parametrize("path", [*PROOFS, *TAMPERED, DEMO_V2], ids=lambda p: p.name)
def test_byte_exact_evidence_has_no_carriage_return(path):
    assert b"\r" not in path.read_bytes(), f"{path.name}: CRLF line endings (see .gitattributes -text)"
