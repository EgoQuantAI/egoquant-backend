"""The advisor chat's second tool, `check_trade`: one possible trade, checked by the deterministic trade check.

A signed-in investor asks the chat about ONE trade ("could I sell half of my VTI?", "what about buying 2,000 of NVDA?").
The model never decides anything. It only turns the question into validated arguments; this module runs the SAME
deterministic check as the Trade check page (egoquant/trade_check.py: the investor's own limits, the independent Risk
Engine, Agent B's limit from the trading history saved in their account) and returns the verdict with its numbers, which
the model may only explain, behind number grounding and the compliance guard.

Read-only: nothing is traded, saved or recorded from the chat (the Trade check page records decisions). The tool needs the
signed-in account (the holdings come from the trades saved there, never from the request), the `trade_check` consent, and a
sealed what-if context that belongs to that same account. Every problem becomes a status the model can explain; nothing raises.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from egoquant.trade_check import VERDICT_WORDS, PlannedTrade, TradeCheckError

logger = logging.getLogger(__name__)

TOOL_NAME = "check_trade"
TIMEOUT_S = 40.0                 # PROVISIONAL: a check rebuilds the investor's context (profile, Agent C prices, Agent B)
RATE_WINDOW_S = 600.0
RATE_RUNS = 10                   # PROVISIONAL: checks per user per window from the chat (the Trade check page has its own limit)
MAX_AMOUNT = 1e9

TRADE_TOOL = {
    "type": "function",
    "name": TOOL_NAME,
    "description": ("Check ONE possible trade against this investor's own limits with EgoQuant's deterministic trade check "
                    "(their risk category, product risk level, the independent Risk Engine and what their saved trading "
                    "history shows). Use it whenever the investor asks about buying or selling a specific product. It "
                    "returns a verdict and numbers; it does not trade and it is not a recommendation. Give the size "
                    "as ONE number: `size_kind` is `amount` when `size` is money (in the account currency) or "
                    "`percent_of_position` when `size` is the share of a product they hold that they would sell "
                    "(for \"half\" write 50). The investor's identity, holdings and limits are filled in by the server."),
    "parameters": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The product's ticker, for example VTI or AAPL."},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "size_kind": {"type": "string", "enum": ["amount", "percent_of_position"],
                          "description": "What `size` means: money, or (only with side=sell) the percent of the current position."},
            "size": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_AMOUNT,
                     "description": "The size of the trade: money in the account currency, or the percent (0 to 100) of the "
                                    "position when size_kind is percent_of_position."},
        },
        "required": ["ticker", "side", "size_kind", "size"],
        "additionalProperties": False,
    },
}


class TradeArgs(BaseModel):
    """The model's arguments are untrusted input: a strict whitelist, no field for limits, category or identity."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=15)
    side: Literal["buy", "sell"]
    amount: float | None = Field(None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    percent_of_position: float | None = Field(None, gt=0, le=100, allow_inf_nan=False)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or not all(ch.isalnum() or ch in ".-" for ch in value):
            raise ValueError("ticker")
        return value

    @model_validator(mode="after")
    def _one_size(self) -> "TradeArgs":
        if (self.amount is None) == (self.percent_of_position is None):
            raise ValueError("size")
        if self.percent_of_position is not None and self.side != "sell":
            raise ValueError("percent")
        return self


# The check's own labels (advisable / not_advisable ...) are internal codes; the model gets the words of egoquant.trade_check.VERDICT_WORDS.


class TradeToolResult(BaseModel):
    """What the model (and the chat card) gets back. `check` is the trade check exactly as the Trade check page shows it."""

    status: Literal["ok", "invalid", "needs_account", "needs_consent", "no_history", "unavailable"]
    reason: str | None = None
    planned: dict | None = None            # ticker, side, amount as checked (a percentage is resolved to money)
    resolved_from_percent: float | None = None
    verdict: str | None = None             # advisable / not_advisable / ... (the check's own words; for the card, not the model)
    verdict_words: str | None = None       # what the model says instead: "within your limits" / "outside your limits" / ...
    check: dict | None = None


# ---------------------------------------------------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = defaultdict(deque)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trade-chat")


def check_rate(key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] > RATE_WINDOW_S:
            hits.popleft()
        if len(hits) >= RATE_RUNS:
            return False
        hits.append(now)
        return True


def for_model(result: TradeToolResult) -> dict:
    """The tool output the model sees: the verdict only as verdict_words (no 'advisable' label anywhere in it)."""
    payload = result.model_dump(mode="json", exclude_none=True)
    payload.pop("verdict", None)
    if isinstance(payload.get("check"), dict):
        payload["check"].pop("verdict", None)
    return payload


def _invalid(reason: str) -> TradeToolResult:
    return TradeToolResult(status="invalid", reason=reason)


_UNREADABLE = ("I couldn't read that as one trade. Name one product (its ticker, for example VTI), say buy or sell, and give "
               "an amount of money or, when selling, a share of what you hold.")


def _one_size(data: dict) -> dict:
    """The model's `size_kind` + `size` become the one size field the rest of the code knows (`amount` or `percent_of_position`).

    Why: with two optional size fields the live model (found by the live red-team, 20 Oct) filled BOTH with positive numbers
    on every call, so no check ever ran. One number cannot be filled twice. The old two-field form is still accepted (tests,
    older clients) and still refuses two sizes."""
    if "size_kind" in data or "size" in data:
        kind, size = data.get("size_kind"), data.get("size")
        rest = {k: v for k, v in data.items() if k not in ("size_kind", "size")}
        if kind in ("amount", "percent_of_position") and kind not in rest and "amount" not in rest and "percent_of_position" not in rest:
            rest[kind] = size
        else:
            rest["size"] = size                                  # conflicting or unknown: the validator refuses it
        data = rest
    return data


def parse_args(args: Any) -> TradeArgs | TradeToolResult:
    """The validated arguments, or an `invalid` result with a plain reason (never the offending value)."""
    try:
        data = json.loads(args) if isinstance(args, (str, bytes)) else args
        if not isinstance(data, dict):
            return _invalid(_UNREADABLE)
        return TradeArgs(**_one_size(data))
    except (ValueError, TypeError, ValidationError) as error:
        text = str(error)
        if "size" in text:
            return _invalid("Give an amount of money or, when selling, a share of your position: one of the two, not both.")
        if "percent" in text:
            return _invalid("A share of a position can only be used when selling; for a purchase, give an amount of money.")
        if "ticker" in text:
            return _invalid("That doesn't look like a product code (ticker): it may only contain letters, digits, '.' and '-'. "
                            "Which product do you mean?")
        return _invalid(_UNREADABLE)


def run_check_trade(what_if_context: str | None, args: Any, *, user_id: str | None, account_token: str | None,
                    timeout_s: float = TIMEOUT_S) -> TradeToolResult:
    """Validate and run one check. Never raises: every problem becomes a status the model can explain."""
    parsed = parse_args(args)
    if isinstance(parsed, TradeToolResult):
        return parsed
    if not account_token or not user_id:
        return TradeToolResult(status="needs_account", reason="A trade question needs a signed-in account with your trading "
                               "history connected: the holdings come from there. Sign in, connect your history on the "
                               "Account page, and ask again, or use the Trade check page.")
    try:
        from egoquant.agents import what_if as wi
        base = wi.open_what_if_context(what_if_context)
    except Exception:                                              # not sealed by us, expired or tampered with
        return _invalid("Your portfolio could not be verified (it may be old). Rebuild it on the Portfolio page and ask again.")
    if base.get("user_id") != user_id:
        return _invalid("This portfolio was built before you signed in, or under a different account. Rebuild it on the "
                        "Portfolio page while signed in and ask again.")
    try:
        from egoquant.accounts.routes import has_consent
        from egoquant.accounts.store import get_store
        user = get_store().user(account_token)
        if not has_consent(account_token, user, "trade_check"):
            return TradeToolResult(status="needs_consent", reason="Please agree to the trade-check terms on the Trade check page "
                                   "first; then I can check trades for you here.")
    except HTTPException as error:
        logger.info("trade chat: account lookup refused (HTTP %s)", error.status_code)
        return TradeToolResult(status="unavailable", reason="Your account could not be reached right now. Please try again.")
    except Exception:
        logger.exception("trade chat: account lookup failed")
        return TradeToolResult(status="unavailable", reason="Your account could not be reached right now. Please try again.")
    if not check_rate(f"user:{user_id}"):
        return TradeToolResult(status="unavailable", reason="That is many trade checks in a short time. Please wait a few minutes; "
                               "the Trade check page has its own limit.")
    future = _executor.submit(_run, base, parsed, user, account_token)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        logger.warning("trade chat: check timed out after %.0f s", timeout_s)
        return TradeToolResult(status="unavailable", reason="The check took too long. Please try again in a moment.")
    except Exception:                                              # never crash the chat; the cause is logged, not shown
        logger.exception("trade chat: check failed")
        return TradeToolResult(status="unavailable", reason="The check could not run right now. Please try again.")


def _run(base: dict, args: TradeArgs, user, account_token: str) -> TradeToolResult:
    import egoquant_routes as routes
    from egoquant.accounts.ibkr_input import rows_to_report

    store = routes.get_store()
    own = {"user_id": f"eq.{user.id}", "order": "trade_date.asc"}
    trades = store.select(account_token, "trades", own)
    if not trades:
        return TradeToolResult(status="no_history", reason="No trading history is connected to your account yet. Connect your "
                               "IBKR history on the Account page; then I can check trades against what you hold.")
    flows = store.select(account_token, "cash_flows", {"user_id": f"eq.{user.id}", "order": "day.asc"})
    stored = (rows_to_report(trades, flows), "account")
    inputs = {k: v for k, v in dict(base["inputs"]).items() if k not in ("ibkr_report", "ibkr_flex", "ibkr_filename")}
    inputs["current_holdings"] = None
    inputs["goal_amount"], inputs["monthly_flow"] = None, 0.0
    placeholder = 1.0 if args.amount is None else args.amount
    body = routes.TradeCheckIn(**inputs, planned=PlannedTrade(ticker=args.ticker, side=args.side, amount=placeholder))
    resolved: dict = {}

    def resolve(planned: PlannedTrade, holdings: dict[str, float]) -> PlannedTrade:
        if args.percent_of_position is None:
            return planned
        held = holdings.get(planned.ticker, 0.0)
        if held <= 1e-9:
            raise TradeCheckError(f"You do not hold {planned.ticker} in your saved history, so there is nothing to sell. "
                                  "To check buying it, give an amount of money.")
        amount = round(held * args.percent_of_position / 100.0, 2)
        resolved["percent"] = args.percent_of_position
        return planned.model_copy(update={"amount": max(amount, 0.01)})

    try:
        check = routes._trade_check_for(body, stored, resolve_planned=resolve)
    except HTTPException as error:
        if 400 <= error.status_code < 500:
            detail = error.detail if isinstance(error.detail, str) else "The trade cannot be checked as given."
            return _invalid(detail)
        logger.warning("trade chat: check failed with HTTP %s", error.status_code)
        return TradeToolResult(status="unavailable", reason="Market data is unavailable right now. Please try again.")
    dumped = check.model_dump(mode="json")
    return TradeToolResult(status="ok", planned=dumped.get("planned"), resolved_from_percent=resolved.get("percent"),
                           verdict=check.verdict, verdict_words=VERDICT_WORDS.get(check.verdict), check=dumped)


__all__ = ["TOOL_NAME", "TRADE_TOOL", "TradeArgs", "TradeToolResult", "VERDICT_WORDS", "for_model", "parse_args",
           "run_check_trade"]
