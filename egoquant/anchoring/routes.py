"""On-chain sealing endpoints (contract: docs/ON_CHAIN_COORDINATION.md section 6). Included from main.py by Onur.

Owner routes reuse the accounts helpers (imported, never edited): identity comes only from the Supabase token,
every read and write runs AS the user (Row-Level Security) and is filtered by user_id here too. There is no
service key, so a pending seal is retried when its owner is present (the seal badge polls GET .../seal).

Rules: only an approved report is ever confirmed or sealed; confirming never waits on the chain longer than the
budget and succeeds whatever the chain does; the seal never changes a number, limit or verdict; when sealing is not
configured every route except /anchoring/status answers 503 and the frontend hides the feature. /verify stores
nothing and logs nothing.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import UUID

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from egoquant.accounts.routes import _call, _own, _signed_in, log_action
from egoquant.accounts.store import AccountError, User, get_store
from egoquant.anchoring import config as cfg
from egoquant.anchoring.canonical import canonical_text, report_sha256
from egoquant.anchoring.client import (TX_HASH, AnchorClient, AnchorResult, RpcError, anchor_client_from_env,
                                       explorer_tx_url, published_addresses)
from egoquant.anchoring.commit import commitment, new_salt
from egoquant.anchoring.html import seal_section_html, with_seal_section
from egoquant.anchoring.sealed import document_sha256, proof_v2, sealed_document, sealed_report_html
from egoquant.anchoring.proof import ProofError, make_proof, verify_proof
from egoquant.anchoring.record import NotSealable, build_record, check_sealable
from egoquant.briefing.final import news_is_final

logger = logging.getLogger(__name__)
router = APIRouter(tags=["anchoring"])

NOT_AVAILABLE = "Plan sealing is not available on this server."
NEWS_NOT_FINAL = ("The news for this report is still being rated. Confirm it once the rating has finished: the seal "
                  "covers the finished report.")
CHAIN_DOWN = "The blockchain could not be reached. Please try again in a minute."
_UNSET = object()
_client: object = _UNSET
_client_lock = Lock()
_sends: deque = deque()                       # times of seals sent by this process (global daily cap)
_verify_ip: dict[str, deque] = {}
_verify_all: deque = deque()
_rate_lock = Lock()


# --------------------------------------------------------------------------- client (tests replace it)
def get_anchor_client() -> AnchorClient | None:
    global _client
    with _client_lock:
        if _client is _UNSET:
            _client = anchor_client_from_env()
        return _client  # type: ignore[return-value]


def set_anchor_client(client: AnchorClient | None | object = _UNSET) -> None:
    """Tests: a FakeAnchorClient, None (= not configured), or no argument to read the environment again."""
    global _client
    with _client_lock:
        _client = client
        _sends.clear()
        _verify_ip.clear()
        _verify_all.clear()


def _require_client() -> AnchorClient:
    client = get_anchor_client()
    if client is None:
        raise HTTPException(503, NOT_AVAILABLE)
    return client


# --------------------------------------------------------------------------- models and helpers
class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_version: str = Field(min_length=1, max_length=32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seal_out(client: AnchorClient, row: dict | None) -> dict:
    row = row or {"status": "pending"}
    return {"status": row.get("status", "pending"), "tx_hash": row.get("tx_hash"), "chain_id": client.chain_id,
            "block_number": row.get("block_number"), "block_time": _z(row.get("block_time")),
            "sealing_address": row.get("sealing_address") or client.address,
            "explorer_url": explorer_tx_url(client.explorer, row.get("tx_hash")), "error_kind": row.get("error_kind")}


def _z(value) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _own_report(token: str, user: User, report_id: str) -> dict:
    rows = _call(get_store().select, token, "reports", _own(user, id=f"eq.{report_id}", select="id,report", limit="1"))
    if not rows:
        raise HTTPException(404, "Report not found.")
    return rows[0].get("report") or {}


def _record_for(token: str, user: User, report_id: str) -> dict | None:
    rows = _call(get_store().select, token, "advisory_records",
                 _own(user, report_id=f"eq.{report_id}", order="record_version.desc", limit="1"))
    return max(rows, key=lambda r: r.get("record_version", 1)) if rows else None


def _seals(token: str, user: User, record_id) -> list[dict]:
    rows = _call(get_store().select, token, "record_seals",
                 _own(user, record_id=f"eq.{record_id}", order="at.desc", limit="50"))
    return sorted(rows, key=lambda r: (str(r.get("at") or ""), _num(r.get("id"))))


def _num(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _username(token: str, user: User) -> str | None:
    rows = _call(get_store().select, token, "profiles", _own(user, select="username", limit="1"))
    name = (rows[0].get("username") if rows else None) or None
    return str(name) if name else None


def _proof_fields(record: dict, latest: dict | None, chain_id: int, address: str) -> dict:
    return {"record": record["record_canonical"], "salt": record["salt_hex"], "commitment": record["commitment_hex"],
            "chain_id": chain_id, "tx_hash": (latest or {}).get("tx_hash"),
            "sealing_address": (latest or {}).get("sealing_address") or address}


def _sent(token: str, user: User, record: dict) -> dict | None:
    """The latest seal row that carries a transaction (a pending one has none)."""
    rows = [s for s in _seals(token, user, record["id"]) if s.get("tx_hash") and s.get("status") != "pending"]
    return rows[-1] if rows else None


def _insert_seal(token: str, user: User, record: dict, client: AnchorClient, result: AnchorResult) -> dict:
    row = {"user_id": user.id, "record_id": record["id"], "status": result.status, "chain_id": client.chain_id,
           "sealing_address": client.address, "tx_hash": result.tx_hash, "block_number": result.block_number,
           "block_time": result.block_time, "error_kind": result.error_kind}
    out = _call(get_store().insert, token, "record_seals", [row])
    return out[0] if out else row


def _log(token: str, user: User, action: str, details: dict) -> None:
    """Best effort: a log row never fails a confirm or a seal (before 006 the database refuses new actions)."""
    try:
        log_action(token, user, action, details)
    except HTTPException:
        logger.warning("operation_log %s not written", action)


def _under_caps(token: str, user: User, client: AnchorClient) -> bool:
    settings = cfg.settings_from_env()
    now = time.monotonic()
    with _rate_lock:
        while _sends and now - _sends[0] > 86_400:
            _sends.popleft()
        if len(_sends) >= settings.max_per_day:
            return False
    since = (_now() - timedelta(days=1)).isoformat()
    mine = _call(get_store().select, token, "record_seals",
                 _own(user, status="eq.submitted", at=f"gt.{since}", select="id", limit="100"))
    return len(mine) < settings.max_per_user_day


def _send(token: str, user: User, record: dict, client: AnchorClient, budget_s: float) -> dict:
    """One seal attempt for an existing record, with the SAME commitment. Writes a seal row either way."""
    report_id = record["report_id"]
    if not _under_caps(token, user, client):
        row = _insert_seal(token, user, record, client, AnchorResult("pending", error_kind="cap"))
        _log(token, user, "seal_pending", {"report_id": report_id, "kind": "cap"})
        return row
    written: dict = {}

    def signed(tx_hash: str) -> None:                 # the hash is known before the send: store it first
        written["row"] = _insert_seal(token, user, record, client, AnchorResult("submitted", tx_hash=tx_hash))

    try:
        with client.budget(max(budget_s, 0.5)):
            result = client.anchor(bytes.fromhex(record["commitment_hex"]), on_signed=signed)
    except HTTPException:                             # the store refused the row: nothing was sent
        result = AnchorResult("pending", error_kind="store")
    if result.status == "submitted":
        with _rate_lock:
            _sends.append(time.monotonic())
        _log(token, user, "record_sealed", {"report_id": report_id, "tx_hash": result.tx_hash})
        return written.get("row") or _insert_seal(token, user, record, client, result)
    row = _insert_seal(token, user, record, client, AnchorResult("pending", error_kind=result.error_kind))
    _log(token, user, "seal_pending", {"report_id": report_id, "kind": result.error_kind or "unknown"})
    return row


def _refresh(token: str, user: User, record: dict, client: AnchorClient, budget_s: float) -> dict | None:
    """The lazy update behind GET .../seal: re-send a pending or failed seal, read the receipt of a submitted one."""
    seals = _seals(token, user, record["id"])
    latest = seals[-1] if seals else None
    if latest is None or latest.get("status") in ("pending", "failed"):
        return _send(token, user, record, client, budget_s)
    if latest.get("status") != "submitted" or not latest.get("tx_hash"):
        return latest
    with client.budget(budget_s):
        st = client.status(latest["tx_hash"])
    if st.status in ("confirmed", "failed"):
        return _insert_seal(token, user, record, client, st)
    if st.status == "pending" and st.error_kind == "not_found":   # the node never saw it: re-send after a while
        at = datetime.fromisoformat(str(latest.get("at")).replace("Z", "+00:00")) if latest.get("at") else _now()
        if (_now() - at).total_seconds() > cfg.RESEND_AFTER_S:
            return _send(token, user, record, client, budget_s)
    return latest


def _record_out(client: AnchorClient, record: dict, seal: dict | None) -> dict:
    confirmed_at = json.loads(record["record_canonical"]).get("confirmed_at")
    return {"record_id": str(record["id"]), "commitment": record["commitment_hex"], "confirmed_at": confirmed_at,
            "seal": _seal_out(client, seal)}


# --------------------------------------------------------------------------- public status
@router.get("/anchoring/status")
def anchoring_status():
    client = get_anchor_client()
    if client is None:
        return {"enabled": False}
    return {"enabled": True, "chain_id": client.chain_id, "network": "Sepolia testnet",
            "sealing_addresses": published_addresses(client), "explorer": client.explorer,
            "consent_version": cfg.seal_consent_version(), "consent_text": cfg.CONSENT_TEXT}


# --------------------------------------------------------------------------- owner routes
@router.post("/account/reports/{report_id}/confirm")
def confirm_plan(report_id: UUID, body: ConfirmIn, authorization: str | None = Header(None)):
    """'I confirm this plan': record + salt + commitment stored, then one seal attempt within the budget."""
    started = time.monotonic()
    client = _require_client()
    token, user = _signed_in(authorization)
    rid = str(report_id)
    saved = _own_report(token, user, rid)
    try:
        check_sealable(saved)
    except NotSealable:
        raise HTTPException(409, "Only an approved plan can be confirmed.") from None
    existing = _record_for(token, user, rid)
    if existing is None and not news_is_final(saved.get("report")):
        raise HTTPException(409, NEWS_NOT_FINAL)               # the seal fingerprints the FINISHED saved report
    if existing:
        seals = _seals(token, user, existing["id"])
        return {"confirmed": True, "already_confirmed": True, **_record_out(client, existing, seals[-1] if seals else None)}
    version = cfg.seal_consent_version()
    if body.consent_version != version:
        raise HTTPException(409, "The consent text changed. Please read it again and confirm.")
    if not isinstance((saved.get("inputs") or {}).get("answers"), dict):
        raise HTTPException(409, "This report was saved before plans could be sealed. Build the plan again to "
                                 "confirm it.")
    store = get_store()
    confirmed_at = _now().replace(microsecond=0)
    _call(store.insert, token, "consents", [{"user_id": user.id, "scope": "record_seal", "version": version}])
    _log(token, user, "plan_confirmed", {"report_id": rid})
    log_rows = _call(store.select, token, "operation_log",
                     _own(user, select="at,action,details", order="at.asc", limit="500"))
    username = _username(token, user)
    if not username:
        raise HTTPException(409, "Your account has no username yet. Choose one on the Account page, then confirm.")
    # Seal v2: the final client report rendered ONCE (the sealed document), stored byte-exact, fingerprinted in the record.
    try:
        document = sealed_document(saved.get("report") or {}, username=username, report_id=rid)
    except Exception:
        raise HTTPException(422, "This report could not be read.") from None
    def record_row(v2: bool) -> dict:
        try:
            record = build_record(saved, report_id=rid, log_rows=[r for r in log_rows if r.get("at")],
                                  consent_version=version, confirmed_at=confirmed_at,
                                  **({"username": username, "document_sha256": document_sha256(document)} if v2 else {}))
        except NotSealable:
            raise HTTPException(409, "Only an approved plan can be confirmed.") from None
        text = canonical_text(record)
        salt = new_salt()
        row = {"user_id": user.id, "report_id": rid, "record_version": record["record_version"],
               "schema": record["schema"], "record_canonical": text, "salt_hex": salt.hex(),
               "commitment_hex": commitment(salt, text.encode("utf-8")).hex(),
               "report_sha256": record["versions"]["report_sha256"]}
        return {**row, "sealed_document": document} if v2 else row

    row = record_row(v2=True)
    try:
        try:
            inserted = store.insert(token, "advisory_records", [row])
        except AccountError as err:
            # The database refused the v2 row (migration 008 not applied yet: no sealed_document column or grant).
            # The plan is never blocked by that: it is sealed in the v1 format, which verifies as "older seal format".
            if err.status not in (403, 502):
                raise
            row = record_row(v2=False)
            inserted = store.insert(token, "advisory_records", [row])
    except AccountError as err:
        if err.status == 409:                         # a second confirm won the race: answer with its record
            existing = _record_for(token, user, rid)
            if existing:
                seals = _seals(token, user, existing["id"])
                return {"confirmed": True, "already_confirmed": True,
                        **_record_out(client, existing, seals[-1] if seals else None)}
        raise HTTPException(err.status, str(err)) from None
    record_row = inserted[0] if inserted else row
    budget = cfg.settings_from_env().budget_s - (time.monotonic() - started)
    seal = _send(token, user, record_row, client, budget)
    return {"confirmed": True, "already_confirmed": False, **_record_out(client, record_row, seal)}


@router.get("/account/reports/{report_id}/seal")
def seal_status(report_id: UUID, authorization: str | None = Header(None)):
    client = _require_client()
    token, user = _signed_in(authorization)
    record = _record_for(token, user, str(report_id))
    if record is None:
        raise HTTPException(404, "This plan has not been confirmed.")
    seal = _refresh(token, user, record, client, cfg.settings_from_env().budget_s)
    return _record_out(client, record, seal)


@router.get("/account/reports/{report_id}/proof")
def proof_file(report_id: UUID, authorization: str | None = Header(None)):
    client = _require_client()
    token, user = _signed_in(authorization)
    rid = str(report_id)
    record = _record_for(token, user, rid)
    if record is None:
        raise HTTPException(404, "This plan has not been confirmed.")
    latest = _sent(token, user, record)
    if record.get("sealed_document"):                 # v2: the technical seal file carries the sealed document
        fields = _proof_fields(record, latest, client.chain_id, client.address)
        return JSONResponse(proof_v2(fields, record["sealed_document"]),
                            headers={"Content-Disposition": f'attachment; filename="egoquant-seal-{rid}.json"',
                                     "Cache-Control": "no-store"})
    saved = _own_report(token, user, rid)
    report = saved if report_sha256(saved) == record.get("report_sha256") else None   # never a changed copy
    proof = make_proof(record_text=record["record_canonical"], salt=bytes.fromhex(record["salt_hex"]),
                       commit=bytes.fromhex(record["commitment_hex"]), chain_id=client.chain_id,
                       tx_hash=latest.get("tx_hash") if latest else None,
                       sealing_address=(latest or {}).get("sealing_address") or client.address, report=report)
    return JSONResponse(proof, headers={"Content-Disposition": f'attachment; filename="egoquant-proof-{rid}.json"',
                                        "Cache-Control": "no-store"})


@router.get("/account/reports/{report_id}/download")
def report_download(report_id: UUID, job: str | None = Query(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
                    authorization: str | None = Header(None)):
    """The saved report as one HTML page, with the finished news and (when sealing is on) its Seal section.

    job: a finished briefing job for THIS report (checked by its digest): the page shows that job's finished news, also
    when the saved copy could not keep it (migration 004 missing, or a briefing too large to store). Nothing is saved
    here. Without a job (or with someone else's), the saved version. Works whether or not sealing is switched on."""
    from egoquant.briefing import final
    from egoquant.briefing.jobs import get_job
    token, user = _signed_in(authorization)
    rid = str(report_id)
    saved = _own_report(token, user, rid)
    client = get_anchor_client()
    sealed = _sealed_report(token, user, rid, client)
    if sealed is not None:                            # v2: the stored sealed document, never rendered again
        return HTMLResponse(sealed, headers={
            "Content-Disposition": f'attachment; filename="egoquant-sealed-report-{rid[:8]}.html"',
            "Cache-Control": "no-store"})
    report = saved.get("report") or {}
    if job:                                               # the finished news of THIS report's job (full size)
        try:
            report = final.apply_final(report, final.final_sections(final.check_job(get_job(job), report)))
        except final.FinalBriefingError:
            pass                                          # not this report's job, or not finished: the saved version
    try:
        page = final.render_report_html(report)
    except Exception:
        raise HTTPException(422, "This report could not be read.") from None
    if client is not None:
        try:
            record = _record_for(token, user, rid)
            seals = _seals(token, user, record["id"]) if record else []
            page = with_seal_section(page, seal_section_html(client, record, seals[-1] if seals else None))
        except HTTPException:
            logger.warning("seal section left out of a download (seal tables not reachable)")
    name = f"egoquant-report-{str(report.get('as_of') or 'saved')}-{rid[:8]}.html"
    return HTMLResponse(page, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _sealed_report(token: str, user: User, rid: str, client: AnchorClient | None) -> str | None:
    """The downloadable sealed report of a v2 seal: the exact stored document + the footer. None otherwise."""
    try:
        record = _record_for(token, user, rid)
    except HTTPException:
        return None                                   # seal tables not in this database yet
    if not record or not record.get("sealed_document"):
        return None
    latest = _sent(token, user, record)
    seals = _seals(token, user, record["id"])
    shown = seals[-1] if seals else None
    chain_id = client.chain_id if client else cfg.SEPOLIA_CHAIN_ID
    if client is None:                                # sealing switched off since: still the same file, plain words
        from types import SimpleNamespace
        client = SimpleNamespace(explorer=cfg.DEFAULT_EXPLORER, address=(latest or {}).get("sealing_address") or "")
    fields = _proof_fields(record, latest, chain_id, client.address)
    return sealed_report_html(record["sealed_document"], fields, seal_section_html(client, record, shown))


@router.get("/account/seals")
def my_seals(authorization: str | None = Header(None)):
    """My seals: each confirmed report with its block and transaction, newest first (owner only)."""
    client = _require_client()
    token, user = _signed_in(authorization)
    records = _call(get_store().select, token, "advisory_records",
                    _own(user, select="id,report_id,record_canonical,created_at", order="created_at.desc", limit="200"))
    out = []
    for r in records:
        seals = _seals(token, user, r["id"])
        seal = _seal_out(client, seals[-1] if seals else None)
        try:
            confirmed_at = json.loads(r["record_canonical"]).get("confirmed_at")
        except ValueError:
            confirmed_at = None
        out.append({"report_id": str(r["report_id"]), "confirmed_at": confirmed_at, **{k: seal[k] for k in (
            "status", "tx_hash", "block_number", "block_time", "explorer_url")}})
    out.sort(key=lambda x: x["confirmed_at"] or "", reverse=True)
    return {"seals": out}


@router.post("/account/reports/{report_id}/viewed")
def report_viewed(report_id: UUID, authorization: str | None = Header(None)):
    """Logs `report_viewed` once per report (repeat calls write nothing)."""
    _require_client()
    token, user = _signed_in(authorization)
    rid = str(report_id)
    _own_report(token, user, rid)
    rows = _call(get_store().select, token, "operation_log",
                 _own(user, action="eq.report_viewed", select="details", limit="500"))
    if not any((r.get("details") or {}).get("report_id") == rid for r in rows):
        _log(token, user, "report_viewed", {"report_id": rid})
    return {"ok": True}


# --------------------------------------------------------------------------- public verification
def _verify_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        hits = _verify_ip.setdefault(ip, deque())
        for q in (hits, _verify_all):
            while q and now - q[0] > 60:
                q.popleft()
        if len(hits) >= cfg.VERIFY_PER_MINUTE or len(_verify_all) >= cfg.VERIFY_GLOBAL_PER_MINUTE:
            raise HTTPException(429, "Too many checks. Please wait a minute.", headers={"Retry-After": "60"})
        hits.append(now)
        _verify_all.append(now)
        if len(_verify_ip) > 10_000:                  # keep memory bounded
            _verify_ip.clear()


@router.get("/verify/tx/{tx_hash}")
def verify_tx(tx_hash: str, request: Request):
    """The chain's view of one transaction, for the /verify page (which hashes the proof in the browser)."""
    if not TX_HASH.fullmatch(tx_hash):
        raise HTTPException(422, "That is not a transaction hash.")
    client = _require_client()
    _verify_limit(request)
    try:
        with client.budget(cfg.VERIFY_RPC_BUDGET_S):
            tx = client.read_tx(tx_hash)
    except RpcError:
        raise HTTPException(503, CHAIN_DOWN) from None
    if tx is None:
        return {"found": False, "status": "not_found", "chain_id": None, "from": None, "to": None,
                "sender_ok": False, "prefix_ok": False, "commitment": None, "block_number": None, "block_time": None}
    allowed = {a.lower() for a in published_addresses(client)}
    return {"found": True, "status": tx.status, "chain_id": tx.chain_id, "from": tx.sender, "to": tx.to,
            "sender_ok": tx.sender.lower() in allowed and (tx.to or "").lower() == tx.sender.lower(),
            "prefix_ok": tx.commitment is not None, "commitment": tx.commitment.hex() if tx.commitment else None,
            "block_number": tx.block_number, "block_time": tx.block_time,
            "explorer_url": explorer_tx_url(client.explorer, tx_hash)}


@router.post("/verify")
def verify(request: Request, proof: dict = Body(...)):
    """Checks a proof file against the chain. Stores nothing and logs nothing (the record holds answers)."""
    client = _require_client()
    _verify_limit(request)
    try:
        with client.budget(cfg.VERIFY_RPC_BUDGET_S):
            return verify_proof(proof, client.read_tx, published_addresses(client), explorer=client.explorer)
    except ProofError as err:
        raise HTTPException(422, str(err)) from None
    except RpcError:
        raise HTTPException(503, CHAIN_DOWN) from None
