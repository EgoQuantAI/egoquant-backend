"""Proof files and their verification. Verification stores nothing and logs nothing.

egoq-proof-1: record + salt + commitment + tx (+ the saved report JSON). The older format: no document fingerprint.
egoq-proof-2: the same + the SEALED DOCUMENT (egoquant/anchoring/sealed.py); its SHA-256 must equal the record's
versions.report_document_sha256. A sealed report (.html) is checked the same way (verify_sealed_report)."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from egoquant.anchoring.canonical import CanonicalError, report_sha256
from egoquant.anchoring.client import TxInfo, explorer_tx_url
from egoquant.anchoring.commit import HEX64, commitment
from egoquant.anchoring.config import ALLOWED_CHAIN_IDS, DEFAULT_EXPLORER

PROOF = "egoq-proof-1"
PROOF_V2 = "egoq-proof-2"
TX_HASH_LEN = 66

# The order of the checks; the first one that fails names the verdict.
FAILURES = {"document": "report changed", "hash": "hash differs", "tx_found": "tx not found", "chain": "wrong chain",
            "sender": "not sent by EgoQuant", "prefix": "wrong prefix", "commitment": "hash differs",
            "confirmed": "not yet in a block", "report": "report differs"}


class ProofError(ValueError):
    """The file is not a proof file. The message is a fixed, user-safe sentence (never the file's content)."""


def make_proof(*, record_text: str, salt: bytes, commit: bytes, chain_id: int, tx_hash: str | None,
               sealing_address: str, report: dict | None = None) -> dict:
    proof = {"proof": PROOF, "record": record_text, "salt": salt.hex(), "commitment": commit.hex(),
             "chain_id": chain_id, "tx_hash": tx_hash, "sealing_address": sealing_address}
    if report is not None:
        proof["report"] = report
    return proof


def parse_proof(proof: object) -> dict:
    if not isinstance(proof, dict) or proof.get("proof") not in (PROOF, PROOF_V2):
        raise ProofError("This is not an EgoQuant proof file.")
    record, salt, commit, tx = proof.get("record"), proof.get("salt"), proof.get("commitment"), proof.get("tx_hash")
    if not isinstance(record, str) or not (isinstance(salt, str) and HEX64.fullmatch(salt)) \
            or not (isinstance(commit, str) and HEX64.fullmatch(commit)):
        raise ProofError("The proof file is incomplete.")
    if tx is not None and not (isinstance(tx, str) and len(tx) == TX_HASH_LEN and tx.startswith("0x")
                               and HEX64.fullmatch(tx[2:])):
        raise ProofError("The proof file has an invalid transaction hash.")
    if "report" in proof and not isinstance(proof["report"], dict):
        raise ProofError("The proof file's report is not readable.")
    if proof.get("proof") == PROOF_V2 and not isinstance(proof.get("document"), str):
        raise ProofError("The seal file has no sealed report in it.")
    return proof


def verify_proof(proof: object, read_tx: Callable[[str], TxInfo | None], addresses: list[str], *,
                 explorer: str = DEFAULT_EXPLORER) -> dict:
    """The /verify answer for one proof file. `read_tx` reads a transaction by hash (may raise RpcError)."""
    p = parse_proof(proof)
    recomputed = commitment(bytes.fromhex(p["salt"]), p["record"].encode("utf-8"))
    checks: dict[str, bool | None] = {k: None for k in FAILURES}
    try:
        record = json.loads(p["record"])
    except ValueError:
        record = {}
    sealed_doc_hash = (record.get("versions") or {}).get("report_document_sha256") if isinstance(record, dict) else None
    if p["proof"] == PROOF_V2:                       # the sealed document must be exactly the one fingerprinted
        checks["document"] = bool(sealed_doc_hash) and \
            hashlib.sha256(p["document"].encode("utf-8")).hexdigest() == sealed_doc_hash
    checks["hash"] = recomputed.hex() == p["commitment"]
    tx: TxInfo | None = None
    if p.get("tx_hash"):
        tx = read_tx(p["tx_hash"])                 # RpcError propagates: the route answers 503
    checks["tx_found"] = tx is not None
    if tx is not None:
        allowed = {a.lower() for a in addresses}
        checks["chain"] = tx.chain_id in ALLOWED_CHAIN_IDS and tx.chain_id == p.get("chain_id")
        checks["sender"] = tx.sender.lower() in allowed and (tx.to or "").lower() == tx.sender.lower()
        on_chain = tx.commitment
        checks["prefix"] = on_chain is not None
        checks["commitment"] = on_chain == recomputed
        checks["confirmed"] = tx.status == "confirmed"
    if "report" in p:
        try:
            sealed = json.loads(p["record"]).get("versions", {}).get("report_sha256")
            checks["report"] = sealed == report_sha256(p["report"])
        except (ValueError, AttributeError, CanonicalError):
            checks["report"] = False
    failed = next((FAILURES[k] for k in FAILURES if checks[k] is False), None)
    return {"match": failed is None and checks["tx_found"] is True, "failed": failed, "checks": checks,
            "tx_hash": p.get("tx_hash"), "block_number": tx.block_number if tx else None,
            "block_time": tx.block_time if tx else None,
            "explorer_url": explorer_tx_url(explorer, p.get("tx_hash")) if tx else None,
            "format": "v2" if p["proof"] == PROOF_V2 else "v1",
            "older_format_note": None if p["proof"] == PROOF_V2 else "older seal format, no document fingerprint",
            "sealed_for": ((record.get("client") or {}).get("username") if isinstance(record, dict) else None),
            "confirmed_at": record.get("confirmed_at") if isinstance(record, dict) else None}


def verify_sealed_report(page: str, read_tx: Callable[[str], TxInfo | None], addresses: list[str], *,
                         explorer: str = DEFAULT_EXPLORER) -> dict:
    """A downloaded sealed report (.html): its document part and embedded proof, checked like a v2 seal file.
    A page with no seal footer: {"sealed": False} ("This report was never sealed.")."""
    from egoquant.anchoring.sealed import split_sealed_report
    parts = split_sealed_report(page)
    if parts is None:
        return {"sealed": False, "match": False, "failed": "never sealed"}
    document, fields = parts
    return {"sealed": True, **verify_proof({**fields, "proof": PROOF_V2, "document": document}, read_tx, addresses,
                                           explorer=explorer)}
