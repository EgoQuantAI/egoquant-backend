"""The investor's IBKR trading history as input: an uploaded Flex report or a Flex Web Service token.

Shared by /portfolio (used once, nothing kept) and /account/trades/import (saved to the user's own rows).
The token is a credential: it is used for one fetch and never stored, logged or echoed. Error messages come
from Bartu's parser and Damla's client, which never quote a value from the report or the token.
"""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from egoquant.agents.behavioral.ibkr import (IbkrCashTransaction, IbkrReport, IbkrReportError, IbkrTrade,
                                             parse_ibkr_report)
from egoquant.agents.behavioral.ibkr_flex import FlexError, fetch_flex_report


class IbkrFlexIn(BaseModel):
    """An IBKR Flex Web Service token + query id. A credential: used for one fetch, never stored, logged or echoed."""
    model_config = ConfigDict(extra="forbid")
    token: SecretStr
    query_id: str = Field(pattern=r"^[0-9]{1,12}$")

    @field_validator("token")
    @classmethod
    def plausible(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not (8 <= len(token) <= 200) or not token.isalnum():       # message never contains the value
            raise ValueError("the Flex token should be 8-200 letters and digits, as IBKR shows it")
        return SecretStr(token)


def load_ibkr_report(report_text: str | None, flex: IbkrFlexIn | None,
                     filename: str | None = None) -> tuple[IbkrReport, str]:
    """(parsed report, source label). Raises HTTPException with a user-safe message. `filename` (an upload's
    name) turns on the parser's extension-vs-content check."""
    if flex is not None:
        try:
            text = fetch_flex_report(flex.token.get_secret_value(), flex.query_id)
        except FlexError as err:                       # user-safe message and a short kind, never the token
            raise HTTPException(429 if err.kind == "rate_limited" else 422, f"IBKR: {err}") from None
        source = "ibkr_flex"
    else:
        text, source = report_text, "ibkr_report"
    try:
        return parse_ibkr_report(text, today=date.today(), filename=filename if flex is None else None), source
    except IbkrReportError as err:
        raise HTTPException(422, f"IBKR report: {err}") from None


def report_to_rows(report: IbkrReport, source: str) -> tuple[list[dict], list[dict]]:
    """(trade rows, cash-flow rows) for the user's tables: only what the analysis needs, no account data."""
    trades = [{"trade_date": t.trade_date.isoformat(), "symbol": t.symbol, "side": t.side, "quantity": t.quantity,
               "price": t.price, "currency": t.currency, "asset_category": t.asset_category,
               "cash_change": t.cash_change, "source": source} for t in report.trades]
    flows = [{"day": c.day.isoformat(), "kind": c.kind, "amount": c.amount, "currency": c.currency,
              "source": source} for c in report.cash_transactions]
    return trades, flows


def rows_to_report(trades: list[dict], flows: list[dict]) -> IbkrReport:
    """The user's stored rows back into an IbkrReport, so the same replay runs on saved data."""
    if not trades:
        raise HTTPException(404, "No trading history is connected to your account yet.")
    parsed = [IbkrTrade(trade_date=date.fromisoformat(str(t["trade_date"])[:10]), symbol=t["symbol"], side=t["side"],
                        quantity=float(t["quantity"]), price=float(t["price"]), currency=t["currency"],
                        asset_category=t["asset_category"], cash_change=float(t["cash_change"])) for t in trades]
    cash = [IbkrCashTransaction(day=date.fromisoformat(str(c["day"])[:10]), kind=c["kind"], amount=float(c["amount"]),
                                currency=c["currency"]) for c in flows]
    days = [t.trade_date for t in parsed] + [c.day for c in cash]
    currencies = sorted({t.currency for t in parsed})
    notes = [] if len(currencies) == 1 else [f"Trades in {len(currencies)} currencies; only {currencies[0]} is used."]
    return IbkrReport(format="xml", currency=currencies[0], trades=[t for t in parsed if t.currency == currencies[0]],
                      cash_transactions=[c for c in cash if c.currency == currencies[0]],
                      start=min(days), end=max(days), notes=notes)
