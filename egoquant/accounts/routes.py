"""Account endpoints (/account/...): the signed-in user's own data only.

Who the user is comes ONLY from their verified Supabase access token (Authorization: Bearer ...); no endpoint
takes a user id. Every read and write goes to Supabase as that user (Row-Level Security) and is also filtered
by user_id here. Sign-up, login, passwords and email confirmation are Supabase Auth's job, not ours.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from egoquant.accounts.ibkr_input import IbkrFlexIn, load_ibkr_report, report_to_rows
from egoquant.accounts.store import SIGNUP_CONSENTS_KEY, AccountError, User, get_store

router = APIRouter(prefix="/account", tags=["account"])

CONSENT_VERSION = "2026-10-19"         # bump when the consent text on the connect screen changes
SEAL_CONSENT_VERSION = "2026-10-20-v2"    # the text of the on-chain "confirm and seal" consent (scope record_seal); separate, so a
                                          # change to it does not ask everybody to agree to the IBKR / trade-check text again
CONSENT_SCOPES = ("ibkr_data", "trade_check")
IMPORTED_SOURCES = ("ibkr_report", "ibkr_flex")
AVATAR_PRESET = re.compile(r"preset:p[1-5]")             # the five built-in pictures (frontend components)
AVATAR_DATA = re.compile(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})")
AVATAR_MAX_BYTES = 64_000                                # the browser sends a 160 px picture, ~10-30 kB
AVATAR_MAGIC = {"jpeg": (b"\xff\xd8\xff",), "png": (b"\x89PNG\r\n\x1a\n",), "webp": (b"RIFF",)}
ONBOARDING_MAX_BYTES = 200_000                           # the table accepts < 256 kB; answers + profile are ~5 kB
_USER_LIMIT, _USER_WINDOW = 30, 60.0   # requests per user per minute on /account
_user_hits: dict[str, deque] = defaultdict(deque)
_username_hits: deque = deque()
_lock = Lock()


# --------------------------------------------------------------------------- request models
class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["ibkr_data", "trade_check"]
    version: str = Field(min_length=1, max_length=32)


class TradesImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ibkr_report: str | None = Field(None, max_length=2_000_000)
    ibkr_filename: str | None = Field(None, max_length=255)    # the upload's name (.xml / .csv)
    ibkr_flex: IbkrFlexIn | None = None


class AvatarIn(BaseModel):
    """A built-in picture ("preset:p1".."preset:p5") or the user's own small raster picture as a data URL."""
    model_config = ConfigDict(extra="forbid")
    avatar: str = Field(min_length=9, max_length=90_000)


def valid_avatar(value: str) -> bool:
    """Presets, or a JPEG/PNG/WebP whose bytes really are that format. Never SVG (it can carry script)."""
    if AVATAR_PRESET.fullmatch(value):
        return True
    m = AVATAR_DATA.fullmatch(value)
    if not m:
        return False
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        return False
    if not 0 < len(raw) <= AVATAR_MAX_BYTES or not raw.startswith(AVATAR_MAGIC[m.group(1)]):
        return False
    return m.group(1) != "webp" or raw[8:12] == b"WEBP"


class SavedResultIn(BaseModel):
    """The built portfolio, by reference: the saved report's id plus the sealed contexts the advisor needs
    (HMAC-sealed by the backend, so a changed one is refused where it is used)."""
    model_config = ConfigDict(extra="forbid")
    report_id: UUID
    chat_context: str | None = Field(None, max_length=60_000)
    what_if_context: str | None = Field(None, max_length=60_000)
    briefing_context: str | None = Field(None, max_length=60_000)


class OnboardingState(BaseModel):
    """Where the user is in onboarding: the frontend's saved session, without the built report itself.
    Display state only: /portfolio re-scores the answers every time and never reads this row."""
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    interests: dict[str, Any] | None = None
    intake: dict[str, Any] | None = None
    riskGame: dict[str, Any] | None = None
    builtAt: str | None = Field(None, max_length=40)
    result: SavedResultIn | None = None
    savedAt: str = Field(min_length=1, max_length=40)


class OnboardingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: OnboardingState


class TradeDecisionIn(BaseModel):
    """What the investor did after a trade check. 'made' records the trade as THEY report it (the real
    execution price and quantity); the app never trades."""
    model_config = ConfigDict(extra="forbid")
    decision: Literal["made", "not_made"]
    quantity: float | None = Field(None, gt=0, le=1e12, allow_inf_nan=False)
    price: float | None = Field(None, gt=0, le=1e9, allow_inf_nan=False)
    trade_date: date | None = None
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- helpers
def _limit(key: str, hits: deque, limit: int) -> None:
    now = time.monotonic()
    with _lock:
        while hits and now - hits[0] > _USER_WINDOW:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(429, "Too many requests. Please wait a minute.", headers={"Retry-After": "60"})
        hits.append(now)


def _signed_in(authorization: str | None) -> tuple[str, User]:
    """(token, user) from 'Authorization: Bearer <token>'. 401 unless Supabase confirms the token."""
    if not (authorization or "").startswith("Bearer "):
        raise HTTPException(401, "Please sign in.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        user = get_store().user(token)
    except AccountError as err:
        raise HTTPException(err.status, str(err)) from None
    _limit(user.id, _user_hits[user.id], _USER_LIMIT)
    return token, user


def _call(fn, *args):
    try:
        return fn(*args)
    except AccountError as err:
        raise HTTPException(err.status, str(err)) from None


def _own(user: User, **extra: str) -> dict[str, str]:
    """PostgREST filter: this user's rows only (RLS enforces the same; this is the second lock)."""
    return {"user_id": f"eq.{user.id}", **extra}


def log_action(token: str, user: User, action: str, details: dict | None = None) -> None:
    """Operation log: user-confirmed actions, never secrets or raw personal data (counts, dates, labels)."""
    _call(get_store().insert, token, "operation_log", [{"user_id": user.id, "action": action,
                                                        "details": details or {}}])


def has_consent(token: str, user: User, scope: str, version: str | None = None) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    rows = _call(get_store().select, token, "consents",
                 _own(user, scope=f"eq.{scope}", version=f"eq.{version or CONSENT_VERSION}", expires_at=f"gt.{now}",
                      select="id", limit="1"))
    return bool(rows)


# --------------------------------------------------------------------------- endpoints
@router.get("/username-available")
def username_available(name: str = Query(min_length=3, max_length=20, pattern=r"^[a-z0-9_]+$")):
    """Is this username free? Yes/no only (never who holds it). Rate-limited for everyone together."""
    _limit("username", _username_hits, 60)
    return {"available": bool(_call(get_store().rpc, None, "username_available", {"name": name}))}


@router.get("/consent-version")
def consent_version():
    """The version of the consent texts, for the sign-up screen (nobody is signed in there yet). Public: no data."""
    return {"version": CONSENT_VERSION, "scopes": list(CONSENT_SCOPES)}


@router.get("/me")
def me(authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    store = get_store()
    now = datetime.now(timezone.utc).isoformat()
    # The four reads are independent: made at the same time, the page waits for one database round trip, not six
    # (the consents of every scope in one read: current version, not expired; the same rule as has_consent).
    reads = {
        "profile": ("profiles", _own(user, select="username,avatar,created_at")),
        "trades": ("trades", _own(user, select="id")),
        "reports": ("reports", _own(user, select="id")),
        "consents": ("consents", _own(user, version=f"eq.{CONSENT_VERSION}", expires_at=f"gt.{now}", select="scope")),
    }
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futures = {key: pool.submit(_call, store.select, token, table, params) for key, (table, params) in reads.items()}
        profile, trades, reports, consents = (futures[key].result() for key in reads)
    given = {row.get("scope") for row in consents}
    return {"username": profile[0]["username"] if profile else None, "name": user.name,
            "avatar": profile[0].get("avatar") if profile else None,
            "consents": {s: s in given for s in CONSENT_SCOPES},
            "consent_version": CONSENT_VERSION, "n_trades": len(trades), "n_reports": len(reports)}


@router.get("/profile")
def profile(authorization: str | None = Header(None)):
    """Name, username and picture only (the sidebar asks on every page, so this stays one small read)."""
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "profiles", _own(user, select="username,avatar"))
    return {"username": rows[0]["username"] if rows else None, "name": user.name,
            "avatar": rows[0].get("avatar") if rows else None}


@router.put("/avatar")
def set_avatar(body: AvatarIn, authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    if not valid_avatar(body.avatar):
        raise HTTPException(422, "Choose one of the pictures, or upload a JPEG, PNG or WebP image under 64 kB.")
    if not _call(get_store().update, token, "profiles", _own(user), {"avatar": body.avatar}):
        raise HTTPException(404, "Profile not found.")
    return {"ok": True}


@router.get("/onboarding")
def get_onboarding(authorization: str | None = Header(None)):
    """The user's saved onboarding (questionnaire, preferences, game, goal inputs), or null before the first step."""
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "onboarding", _own(user, select="state,updated_at", limit="1"))
    return {"state": rows[0]["state"] if rows else None, "updated_at": rows[0].get("updated_at") if rows else None}


@router.put("/onboarding")
def put_onboarding(body: OnboardingIn, authorization: str | None = Header(None)):
    """Replace the user's onboarding row with the current state (one row per user)."""
    token, user = _signed_in(authorization)
    state = body.state.model_dump(mode="json", exclude_none=True)
    if len(json.dumps(state)) > ONBOARDING_MAX_BYTES:
        raise HTTPException(413, "The onboarding answers are too large to save.")
    store = get_store()
    now = datetime.now(timezone.utc).isoformat()
    if not _call(store.update, token, "onboarding", _own(user), {"state": state, "updated_at": now}):
        _call(store.insert, token, "onboarding", [{"user_id": user.id, "state": state}])
    return {"ok": True}


@router.post("/consent")
def give_consent(body: ConsentIn, authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    if body.version != CONSENT_VERSION:
        raise HTTPException(409, "The consent text has changed. Please read it again.")
    _call(get_store().insert, token, "consents", [{"user_id": user.id, "scope": body.scope, "version": body.version}])
    log_action(token, user, "consent_given", {"scope": body.scope, "version": body.version})
    return {"ok": True}


@router.post("/signup-consents")
def apply_signup_consents(authorization: str | None = Header(None)):
    """Record the consents the person ticked on the sign-up screen (kept in their own user_metadata until the first
    sign-in, because with email confirmation nobody is signed in at sign-up). Runs once: the metadata key is removed
    FIRST, so a later "Delete my data" (which erases consents) is never undone by a later login. A consent whose text
    has changed since (other version) is not recorded; the account page asks for it again."""
    token, user = _signed_in(authorization)
    if not user.signup_consents:
        return {"recorded": []}
    _call(get_store().update_user_data, token, {SIGNUP_CONSENTS_KEY: None})
    recorded = []
    for scope, version in user.signup_consents:
        if scope not in CONSENT_SCOPES or version != CONSENT_VERSION or has_consent(token, user, scope):
            continue
        _call(get_store().insert, token, "consents", [{"user_id": user.id, "scope": scope, "version": version}])
        log_action(token, user, "consent_given", {"scope": scope, "version": version, "at": "sign-up"})
        recorded.append(scope)
    return {"recorded": recorded}


@router.post("/trades/import")
def import_trades(body: TradesImportIn, authorization: str | None = Header(None)):
    """Connect IBKR data: parse (or fetch once with a token), keep only what the analysis needs, replace the
    previous import. The file and the token are never stored."""
    token, user = _signed_in(authorization)
    if (body.ibkr_report is None) == (body.ibkr_flex is None):
        raise HTTPException(422, "Send either an IBKR Flex report or a Flex token, not both and not neither.")
    if not has_consent(token, user, "ibkr_data"):
        raise HTTPException(403, "Please agree to the data-use terms before connecting IBKR data.")
    report, source = load_ibkr_report(body.ibkr_report, body.ibkr_flex, body.ibkr_filename)
    trades, flows = report_to_rows(report, source)
    store = get_store()
    for table in ("trades", "cash_flows"):
        _call(store.delete, token, table, _own(user, source=f"in.({','.join(IMPORTED_SOURCES)})"))
    _call(store.insert, token, "trades", [{"user_id": user.id, **t} for t in trades])
    _call(store.insert, token, "cash_flows", [{"user_id": user.id, **f} for f in flows])
    summary = {"source": source, "n_trades": len(trades), "n_cash_flows": len(flows),
               "start": report.start.isoformat(), "end": report.end.isoformat(), "currency": report.currency}
    log_action(token, user, "data_connected", summary)
    return {**summary, "notes": report.notes}


@router.post("/trade-checks/{check_id}/decision")
def record_trade_decision(check_id: UUID, body: TradeDecisionIn, authorization: str | None = Header(None)):
    """'I made this trade' / 'I did not'. Ticker and side come from the saved check, never from this request.
    A made trade is added to the user's trades (source user_recorded) so later checks start from it."""
    token, user = _signed_in(authorization)
    store = get_store()
    rows = _call(store.select, token, "trade_checks", _own(user, id=f"eq.{check_id}", limit="1"))
    if not rows:
        raise HTTPException(404, "Trade check not found.")
    check = rows[0]
    if check.get("user_decision"):
        raise HTTPException(409, "You already recorded what you did for this check.")
    if body.decision == "made":
        if body.quantity is None or body.price is None:
            raise HTTPException(422, "Please enter the quantity and the price you traded at.")
        day = body.trade_date or datetime.now(timezone.utc).date()
        if day > datetime.now(timezone.utc).date() + timedelta(days=1):    # a day's slack for time zones
            raise HTTPException(422, "The trade date cannot be in the future.")
        planned = check["planned"]
        value = body.quantity * body.price
        _call(store.insert, token, "trades", [{
            "user_id": user.id, "trade_date": day.isoformat(), "symbol": planned["ticker"], "side": planned["side"],
            "quantity": body.quantity, "price": body.price, "currency": body.currency, "asset_category": "STK",
            "cash_change": -value if planned["side"] == "buy" else value, "source": "user_recorded"}])
        log_action(token, user, "trade_recorded", {"check_id": str(check_id), "ticker": planned["ticker"],
                                                   "side": planned["side"], "date": day.isoformat()})
    _call(store.update, token, "trade_checks", _own(user, id=f"eq.{check_id}"),
          {"user_decision": body.decision, "decided_at": datetime.now(timezone.utc).isoformat()})
    log_action(token, user, "decision_recorded", {"check_id": str(check_id), "decision": body.decision})
    return {"ok": True, "decision": body.decision}


@router.get("/trades")
def list_trades(authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "trades",
                 _own(user, select="id,trade_date,symbol,side,quantity,price,currency,source", order="trade_date.asc"))
    return {"trades": rows}


@router.get("/reports")
def list_reports(authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "reports",
                 _own(user, select="id,created_at,summary", order="created_at.desc", limit="50"))
    return {"reports": rows}


@router.get("/reports/{report_id}")
def get_report(report_id: UUID, authorization: str | None = Header(None)):
    """One of the user's own reports; someone else's id answers 404, exactly like a missing one."""
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "reports", _own(user, id=f"eq.{report_id}", limit="1"))
    if not rows:
        raise HTTPException(404, "Report not found.")
    return rows[0]


@router.get("/reports/{report_id}/contexts")
def report_contexts(report_id: UUID, authorization: str | None = Header(None)):
    """The Advisor's sealed facts for one of the user's own saved reports, so a saved report can be discussed like the
    build it came from. The row holds the whole /portfolio response except html, chat_context, report_id and
    what_if_context (egoquant_routes.account_portfolio), so build_portfolio_facts sees exactly what it saw at build
    time (with the finished news, if the report took it). It is sealed AS OF the build (iat = created_at): the same
    MAX_AGE_SECONDS applies, so an older report is rebuilt, not discussed, exactly as on the Portfolio page.
    what_if_context: the What-If base facts the build sealed are kept in the row (`what_if_facts`, egoquant_routes.
    account_portfolio); they are re-sealed the same way, with this report's id, and stay bound to the owner. A row
    saved before that (no key) or one whose facts did not fit (None) gets none, and `what_if_note` says which.
    Someone else's id answers 404."""
    from egoquant.advisor import build_portfolio_facts, seal_context
    from egoquant.advisor.context import MAX_AGE_SECONDS
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "reports",
                 _own(user, id=f"eq.{report_id}", select="created_at,report", limit="1"))
    if not rows:
        raise HTTPException(404, "Report not found.")
    saved = dict(rows[0].get("report") or {})
    answers = (saved.pop("inputs", None) or {}).get("answers")       # rows saved before inputs were kept: None
    kept = "what_if_facts" in saved
    what_if_facts = saved.pop("what_if_facts", None)
    what_if_note = None if isinstance(what_if_facts, dict) else "not_kept" if kept else "older_report"
    try:
        built = datetime.fromisoformat(str(rows[0].get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        built = None
    if built is None or time.time() - built > MAX_AGE_SECONDS or not saved.get("report"):
        return {"chat_context": None, "what_if_context": None, "answers": answers, "expired": built is not None,
                "briefing_context": None, "what_if_note": what_if_note}
    try:
        chat = seal_context(build_portfolio_facts(saved), now=built)
    except Exception:
        chat = None                                                   # an unreadable old row: no chat, never a 500
    what_if = None
    if isinstance(what_if_facts, dict) and what_if_facts.get("user_id") in (None, user.id):
        try:
            what_if = seal_context({**what_if_facts, "report_id": str(report_id)}, now=built)
        except Exception:
            what_if_note = "not_kept"
    return {"chat_context": chat, "what_if_context": what_if, "answers": answers, "expired": False,
            "briefing_context": saved.get("briefing_context"), "what_if_note": what_if_note if what_if is None else None}


@router.delete("/reports/{report_id}")
def delete_report(report_id: UUID, authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    if not _call(get_store().delete, token, "reports", _own(user, id=f"eq.{report_id}")):
        raise HTTPException(404, "Report not found.")
    return {"deleted": True}


@router.get("/log")
def operation_log(authorization: str | None = Header(None)):
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "operation_log",
                 _own(user, select="at,action,details", order="at.desc", limit="200"))
    return {"log": rows}


@router.delete("/data")
def delete_my_data(authorization: str | None = Header(None)):
    """Erase every stored row of this user (trades, reports, checks, consents, log). The account stays."""
    token, user = _signed_in(authorization)
    _call(get_store().rpc, token, "delete_my_data", {})
    return {"deleted": True}


@router.delete("")
def delete_my_account(authorization: str | None = Header(None)):
    """Delete the account itself; every row goes with it (ON DELETE CASCADE)."""
    token, user = _signed_in(authorization)
    _call(get_store().rpc, token, "delete_my_account", {})
    return {"deleted": True}
