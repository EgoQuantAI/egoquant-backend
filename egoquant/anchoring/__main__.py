"""On-chain sealing CLI (the fallback when the live deploy is not ready, and the demo tool).

    python -m egoquant.anchoring --balance                     sealing address + Sepolia balance
    python -m egoquant.anchoring demo-record OUT.json [--saved SAVED.json]   a hand-written TEST record (+ OUT.report.json)
    python -m egoquant.anchoring seal RECORD.json [--report SAVED.json] [--out P.proof.json] [--wait 180]
    python -m egoquant.anchoring verify P.proof.json|R.html    the same checks as POST /verify (seal file or sealed report)
    python -m egoquant.anchoring tamper P.proof.json OUT.json  a copy with one weight moved by 1 ppm (demo)
    python -m egoquant.anchoring retry [DIR]                   re-send this machine's unsent proofs

The key comes only from EGOQ_SEAL_PRIVATE_KEY (environment or the backend .env, `--env PATH`). It is never
printed. A proof file holds the record and its salt: keep it like the record itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from egoquant.anchoring.canonical import canonical_text
from egoquant.anchoring.client import (FakeAnchorClient, anchor_client_from_env, explorer_tx_url,
                                       published_addresses, wait_for_block)
from egoquant.anchoring.commit import commitment, new_salt
from egoquant.anchoring.config import seal_consent_version
from egoquant.anchoring.proof import ProofError, make_proof, parse_proof, verify_proof
from egoquant.anchoring.record import build_record

DEMO_REPORT = Path(__file__).with_name("demo_saved_report.json")


def _load_env(path: str | None) -> None:
    from dotenv import find_dotenv, load_dotenv
    if path:
        load_dotenv(Path(path).expanduser(), override=False)
    else:
        load_dotenv(find_dotenv(usecwd=True), override=False)
        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def _client(fake: bool):
    if fake:
        return FakeAnchorClient()
    client = anchor_client_from_env(require_enabled=False)
    if client is None:
        sys.exit("Sealing is not configured: EGOQ_SEAL_PRIVATE_KEY is missing or invalid "
                 "(run scripts/onchain_new_wallet.py).")
    return client


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def cmd_balance(args) -> int:
    client = _client(args.fake)
    wei = client.balance_wei()
    print(f"address  {client.address}")
    print(f"chain    {client.chain_id} (Sepolia testnet)")
    print(f"balance  {wei / 1e18:.6f} test ETH ({wei} wei)")
    return 0


def cmd_demo_record(args) -> int:
    saved = json.loads(Path(args.saved).read_text() if args.saved else DEMO_REPORT.read_text())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    report_id = str(uuid.uuid4())
    stamp = now.isoformat()
    log_rows = [{"at": stamp, "action": a, "details": {"report_id": report_id}}
                for a in ("questionnaire_submitted", "report_generated", "report_viewed")]
    record = build_record(saved, report_id=report_id, log_rows=log_rows, consent_version=seal_consent_version(),
                          confirmed_at=now)
    out = Path(args.out)
    _write(out, record)
    _write(out.with_suffix(".report.json"), saved)
    print(f"test record  {out}  (hand-written test numbers, invented investor, report id {report_id})")
    print(f"saved report {out.with_suffix('.report.json')}")
    return 0


def cmd_seal(args) -> int:
    client = _client(args.fake)
    record = json.loads(Path(args.record).read_text())
    text = canonical_text(record)                 # raises on floats / NaN / non-ASCII keys
    report = json.loads(Path(args.report).read_text()) if args.report else None
    salt = new_salt()
    commit = commitment(salt, text.encode("utf-8"))
    out = Path(args.out or Path(args.record).with_suffix(".proof.json"))
    proof = make_proof(record_text=text, salt=salt, commit=commit, chain_id=client.chain_id, tx_hash=None,
                       sealing_address=client.address, report=report)
    _write(out, proof)                            # written before the send: `retry` can pick it up

    def signed(tx_hash: str) -> None:             # the hash is known before the send: keep it first
        proof["tx_hash"] = tx_hash
        _write(out, proof)

    result = client.anchor(commit, on_signed=signed)
    print(f"proof        {out}")
    print(f"commitment   {commit.hex()}")
    if result.status == "pending":
        print(f"status       pending ({result.error_kind}); run `retry` later")
        return 2
    print(f"tx hash      {result.tx_hash}")
    print(f"etherscan    {explorer_tx_url(client.explorer, result.tx_hash)}")
    if args.fake:
        client.mine()
    final = wait_for_block(client, result.tx_hash, timeout_s=args.wait) if args.wait > 0 else result
    print(f"status       {final.status}" + (f" in block {final.block_number} at {final.block_time}"
                                             if final.block_number else ""))
    return 0 if final.status in ("submitted", "confirmed") else 2


def cmd_verify(args) -> int:
    client = _client(args.fake)
    try:
        if args.proof.endswith((".html", ".htm")):            # a downloaded sealed report (seal v2)
            from egoquant.anchoring.proof import verify_sealed_report
            result = verify_sealed_report(Path(args.proof).read_text(encoding="utf-8"), client.read_tx,
                                          published_addresses(client), explorer=client.explorer)
            if not result.get("sealed", True):
                print("This report was never sealed.")
                return 1
        else:
            proof = json.loads(Path(args.proof).read_text())
            result = verify_proof(proof, client.read_tx, published_addresses(client), explorer=client.explorer)
    except (ProofError, ValueError) as err:
        print(f"Does not match ✗ ({err})")
        return 1
    if result["match"]:
        who = f" · Sealed for: {result['sealed_for']}" if result.get("sealed_for") else ""
        print(f"Unchanged ✓{who} — sealed in block {result['block_number']} on {result['block_time']}"
              + (f" ({result['older_format_note']})" if result.get("older_format_note") else ""))
    else:
        print(f"Does not match ✗ ({result['failed']})")
    for name, ok in result["checks"].items():
        print(f"  {name:<11}{'-' if ok is None else ('ok' if ok else 'FAILED')}")
    if result["explorer_url"]:
        print(f"  etherscan  {result['explorer_url']}")
    return 0 if result["match"] else 1


def cmd_tamper(args) -> int:
    proof = parse_proof(json.loads(Path(args.proof).read_text()))
    record = json.loads(proof["record"])
    alloc = record["allocation"]
    if len(alloc) >= 2:                           # one weight up by 1 ppm, another down: still sums to 100%
        alloc[0]["weight_ppm"] += 1
        alloc[-1]["weight_ppm"] -= 1
        what = f"{alloc[0]['ticker']} +1 ppm, {alloc[-1]['ticker']} -1 ppm"
    elif alloc:                                   # a single holding: it can only move down
        alloc[0]["weight_ppm"] -= 1
        what = f"{alloc[0]['ticker']} -1 ppm"
    else:
        record["record_version"] += 1
        what = "record_version +1"
    proof = {**proof, "record": canonical_text(record)}
    _write(Path(args.out), proof)
    print(f"tampered copy {args.out}: {what}")
    return 0


def cmd_retry(args) -> int:
    client = _client(args.fake)
    n = 0
    for path in sorted(Path(args.dir).glob("*.proof.json")):
        proof = json.loads(path.read_text())
        if proof.get("proof") != "egoq-proof-1":
            continue
        if proof.get("tx_hash"):
            st = client.status(proof["tx_hash"])
            if st.status != "pending":            # known to the chain: nothing to send
                print(f"{path.name}: {st.status}")
                continue
        result = client.anchor(bytes.fromhex(proof["commitment"]),
                               on_signed=lambda h, p=proof, f=path: (p.__setitem__("tx_hash", h), _write(f, p)))
        print(f"{path.name}: {result.status} {result.tx_hash or result.error_kind}")
        n += 1
    print(f"{n} re-sent")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m egoquant.anchoring", description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", help="the .env file with EGOQ_SEAL_PRIVATE_KEY (default: the backend .env)")
    ap.add_argument("--fake", action="store_true", help="use the in-memory fake chain (no network)")
    ap.add_argument("--balance", action="store_true", help="show the sealing address and its balance")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("balance")
    p = sub.add_parser("demo-record"); p.add_argument("out")
    p.add_argument("--saved", help="a saved report JSON to build from (default: the built-in hand-written C3 test record)")
    p = sub.add_parser("seal"); p.add_argument("record"); p.add_argument("--report"); p.add_argument("--out")
    p.add_argument("--wait", type=float, default=180.0, help="seconds to wait for a block (0: don't)")
    p = sub.add_parser("verify"); p.add_argument("proof")
    p = sub.add_parser("tamper"); p.add_argument("proof"); p.add_argument("out")
    p = sub.add_parser("retry"); p.add_argument("dir", nargs="?", default=".")
    args = ap.parse_args(argv)
    _load_env(args.env)
    if args.balance or args.cmd == "balance":
        return cmd_balance(args)
    handlers = {"demo-record": cmd_demo_record, "seal": cmd_seal, "verify": cmd_verify, "tamper": cmd_tamper,
                "retry": cmd_retry}
    if args.cmd not in handlers:
        ap.print_help()
        return 1
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
