"""Interactive Brokers (IBKR) Flex trades report -> Agent B's TradingHistory + final holdings.

    parse_ibkr_report(text)            Flex Query report, XML or CSV -> IbkrReport (trades + cash transactions)
    to_trading_history(report, ...)    replay positions day by day with the prices passed in -> IbkrHistory

What is read: the Trades section (date, symbol, buy/sell, quantity, price, currency, net cash) and the Cash
Transactions section (deposits and withdrawals are external flows; dividends, withholding tax, interest and fees
are cash moving inside the account, not trades and not flows). Everything else in the report (account id, name,
descriptions, other sections) is ignored and never kept, logged or echoed in an error.

Rules:
  * Only a Flex Query report with a Trades section is accepted. Anything else (an Activity Statement, a Flex
    Web Service error page, any other file) is rejected with a user-safe message.
  * Untrusted upload: at most MAX_BYTES and MAX_ROWS; UTF-8 text only (no NUL bytes, no Excel/ZIP/PDF files);
    with a filename, only .xml or .csv, and the extension must match the content. XML is parsed ONLY with
    defusedxml with DTDs forbidden (no XXE, no entity expansion such as "billion laughs"); if defusedxml is
    missing, XML is refused rather than parsed unsafely.
  * Every kept field is validated: symbols and currencies must look like symbols and currencies, so a
    spreadsheet formula ("=...", "+...", "@...") can never pass through to a later export. Other columns
    (account id, descriptions, ...) are ignored, never kept. spreadsheet_safe() neutralises text for exports.
  * Error messages name the section, the row number and the field, never the value.
  * Nothing is stored. The caller passes the text in and gets models back.
  * Only stocks, ETFs, funds, bonds and crypto are replayed. Options, futures and currency conversions are left
    out, and a note says how many.
  * One trading currency per report (no exchange-rate conversion). Cash transactions in other currencies are
    left out, with a note.
  * A day without a market price is valued at the symbol's last trade price, and a note names the symbol.
  * Shares sold before any purchase in the report are assumed to have been held from the start; money spent
    before any deposit in the report is assumed to have been in the account at the start. Both are noted.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from egoquant.agents.behavioral.models import Trade, TradingHistory
from egoquant.models import ASSET_CLASSES

MAX_BYTES = 2_000_000                     # same cap as the upload (Onur's /portfolio field)
MAX_ROWS = 20_000
MAX_VALUE = 1e12                          # PROVISIONAL: largest trade value or cash amount; the API's own ceiling for amounts
CASH_ID = "CASH"                          # the cash column in the positions
MIN_HISTORY_DAYS = 126                    # Agent B's default min_history_days; shorter -> a note
REPLAYED_CATEGORIES = {"STK": "equity", "ETF": "equity", "FUND": "equity", "BOND": "bond", "CRYPTO": "crypto"}
TOL = 1e-6
ALLOWED_EXTENSIONS = {".xml": "xml", ".csv": "csv"}
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9 ./\-]{0,23}")          # IBKR symbols: "VTI", "BRK B", "RDS.A", "BTC"
CURRENCY = re.compile(r"[A-Z]{3}")
BINARY_SIGNATURES = {b"PK\x03\x04": "a ZIP or Excel (.xlsx) file", b"%PDF": "a PDF",
                     b"\xd0\xcf\x11\xe0": "an old Excel (.xls) file"}
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

CashKind = Literal["deposit", "withdrawal", "dividend", "other"]


class IbkrReportError(ValueError):
    """User-safe: the message never contains a value from the report."""


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class IbkrTrade(BaseModel):
    trade_date: date
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    currency: str
    asset_category: str                   # IBKR assetCategory, e.g. STK
    cash_change: float                    # net cash for the account (negative for a buy), commissions included


class IbkrCashTransaction(BaseModel):
    day: date
    kind: CashKind
    amount: float                         # signed: + into the account
    currency: str
    symbol: str | None = None             # e.g. the stock that paid a dividend


class IbkrReport(BaseModel):
    format: Literal["xml", "csv"]
    currency: str                         # the trading currency
    trades: list[IbkrTrade]
    cash_transactions: list[IbkrCashTransaction]
    start: date
    end: date
    notes: list[str] = Field(default_factory=list)


class IbkrHistory(BaseModel):
    history: TradingHistory               # ready for BehavioralData(history=..., data_source="real")
    final_weights: dict[str, float]       # holdings at as_of incl. CASH_ID; sums to 1 (empty if the account is empty)
    final_value: float
    currency: str
    start: date
    end: date
    n_trades: int
    valued_at_trade_price: list[str]      # symbols with days valued at their last trade price
    notes: list[str]                      # plain sentences for the report


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------
def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


TRADE_FIELDS = {"symbol", "buysell", "quantity", "tradeprice", "tradedate", "datetime", "currency", "currencyprimary",
                "assetcategory", "assetclass", "netcash", "proceeds", "ibcommission", "levelofdetail"}
CASH_FIELDS = {"type", "amount", "datetime", "date", "settledate", "reportdate", "currency", "currencyprimary", "symbol"}
HEADER_WORDS = TRADE_FIELDS | CASH_FIELDS | {"clientaccountid", "accountid", "description", "conid", "isin"}


def _get(row: dict[str, str], *names: str) -> str:
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""


def _date(text: str) -> date | None:
    """IBKR date settings: yyyyMMdd (default), yyyy-MM-dd, MM/dd/yyyy; a time part after ';', ',' or ' ' is cut."""
    head = re.split(r"[;, T]", text.strip(), maxsplit=1)[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _number(text: str) -> float | None:
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return value if np.isfinite(value) else None


def _cash_kind(kind: str, amount: float) -> CashKind:
    k = _key(kind)
    if k.startswith("deposit"):                            # "Deposits/Withdrawals", "Deposits & Withdrawals"
        return "deposit" if amount >= 0 else "withdrawal"
    if "dividend" in k or "inlieu" in k:
        return "dividend"
    return "other"                                         # withholding tax, interest, fees: inside the account


# --------------------------------------------------------------------------
# Reading the two formats into plain rows: (section, row number, {field: value})
# --------------------------------------------------------------------------
Row = tuple[str, int, dict[str, str]]


def _has_dtd(text: str) -> bool:
    """First layer: a Flex report never has a DTD. defusedxml (forbid_dtd) is the second, independent layer."""
    return re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE) is not None


def _xml_rows(text: str) -> list[Row]:
    if _has_dtd(text):
        raise IbkrReportError("The file contains XML features a Flex report never uses; it was not read.")
    try:
        from defusedxml import DefusedXmlException
        from defusedxml import ElementTree as SafeET
    except ImportError:                                            # fail closed: never fall back to xml.etree
        raise IbkrReportError("XML reports cannot be read on this server right now. Upload the CSV version.") from None
    try:
        root = SafeET.fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise IbkrReportError("The file contains XML features a Flex report never uses; it was not read.") from None
    except SafeET.ParseError:
        raise IbkrReportError("The file is not valid XML.") from None
    if root.tag == "FlexStatementResponse":                # the Flex Web Service's status/error reply
        raise IbkrReportError("This is a Flex Web Service reply, not a report. Download the report itself.")
    if root.tag != "FlexQueryResponse":
        raise IbkrReportError("This is not an IBKR Flex Query report.")
    statements = root.findall("./FlexStatements/FlexStatement")
    if len(statements) != 1:
        raise IbkrReportError("The report must contain exactly one account statement.")
    st = statements[0]
    if st.find("Trades") is None:
        raise IbkrReportError("The Flex report has no Trades section. Add Trades to your Flex Query.")
    rows: list[Row] = []
    for n, el in enumerate(st.findall("./Trades/Trade"), start=1):
        rows.append(("trades", n, {_key(k): v for k, v in el.attrib.items()}))
    for n, el in enumerate(st.findall("./CashTransactions/CashTransaction"), start=1):
        rows.append(("cash", n, {_key(k): v for k, v in el.attrib.items()}))
    return rows


def _csv_rows(text: str) -> list[Row]:
    rows: list[Row] = []
    header: list[str] | None = None
    section = "other"
    saw_trades = False
    try:                                                           # csv.Error: a field over 128 KB, bad quoting
        lines = list(csv.reader(io.StringIO(text)))                # the text is already capped at 2 MB
    except csv.Error:
        raise IbkrReportError("The file is not a readable CSV report: a field is far too long or its quotes are broken.") from None
    for line_no, cells in enumerate(lines, start=1):
        if not cells or not any(c.strip() for c in cells):
            continue
        first = cells[0].strip().upper()
        if line_no == 1 and first == "STATEMENT":
            raise IbkrReportError("This is an IBKR Activity Statement, not a Flex Query report. "
                                  "Create a Flex Query with Trades and Cash Transactions and download that.")
        if first in {"BOF", "BOA", "BOS", "EOS", "EOA", "EOF"}:      # header/trailer records
            continue
        if first in {"HEADER", "DATA"} and len(cells) > 2:           # "HEADER","TRNT",... layout
            cells = cells[2:]
        keys = [_key(c) for c in cells]
        if sum(k in HEADER_WORDS for k in keys) >= 3:                # a section's header row
            header = keys
            names = set(keys)
            if {"symbol", "quantity", "tradeprice"} <= names:
                section, saw_trades = "trades", True
            elif {"type", "amount"} <= names:
                section = "cash"
            else:
                section = "other"
            continue
        if header is None or section == "other":
            continue
        if len(cells) != len(header):
            raise IbkrReportError(f"Line {line_no}: it has {len(cells)} fields, but its section header has {len(header)}.")
        rows.append((section, line_no, dict(zip(header, cells))))
        if len(rows) > MAX_ROWS:                                     # stop reading early
            raise IbkrReportError(f"The report has more than {MAX_ROWS} rows.")
    if not saw_trades:
        raise IbkrReportError("This is not an IBKR Flex trades report: no Trades section was found "
                              "(it needs Symbol, Quantity and TradePrice columns).")
    return rows


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------
def spreadsheet_safe(value: str) -> str:
    """For any CSV/Excel export of user-supplied text: a cell starting with = + - @ (or a tab / CR) is prefixed
    with an apostrophe so a spreadsheet shows it as text instead of running it as a formula. Plain numbers
    ("-75001", "+3.5") are left alone."""
    if value.startswith(FORMULA_START) and _number(value) is None:
        return "'" + value
    return value


def _check_upload(text: str | bytes, filename: str | None) -> str:
    """Size, type and encoding checks before any parsing. Returns the text."""
    raw = text if isinstance(text, bytes) else None
    if raw is not None:
        if len(raw) > MAX_BYTES:
            raise IbkrReportError("The file is larger than 2 MB.")
        for signature, kind in BINARY_SIGNATURES.items():
            if raw.startswith(signature):
                raise IbkrReportError(f"This is {kind}, not a Flex report. Download the report as XML or CSV.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise IbkrReportError("The file is not UTF-8 text. Download the report as XML or CSV.") from None
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise IbkrReportError("The file is larger than 2 MB.")
    if "\x00" in text:
        raise IbkrReportError("The file is not a text file. Download the report as XML or CSV.")
    text = text.lstrip("﻿").strip()
    if not text:
        raise IbkrReportError("The file is empty.")
    if filename is not None:
        ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            raise IbkrReportError("Only .xml or .csv Flex reports can be uploaded.")
        if ALLOWED_EXTENSIONS[ext] != ("xml" if text.startswith("<") else "csv"):
            raise IbkrReportError("The file's content does not match its extension (.xml or .csv).")
    return text


def parse_ibkr_report(text: str | bytes, *, today: date | None = None, filename: str | None = None) -> IbkrReport:
    """IBKR Flex Query report (XML or CSV) -> IbkrReport. Raises IbkrReportError with a user-safe message.

    filename: the uploaded file's name, when known: only .xml / .csv, matching the content.
    """
    text = _check_upload(text, filename)
    fmt: Literal["xml", "csv"] = "xml" if text.startswith("<") else "csv"
    rows = _xml_rows(text) if fmt == "xml" else _csv_rows(text)
    if len(rows) > MAX_ROWS:
        raise IbkrReportError(f"The report has more than {MAX_ROWS} rows.")

    today = today or date.today()
    errors: list[str] = []
    trades: list[IbkrTrade] = []
    cash: list[IbkrCashTransaction] = []
    skipped_categories = skipped_detail = 0
    where = "Line" if fmt == "csv" else "Row"

    for section, n, row in rows:
        label = f"{'Trades' if section == 'trades' else 'Cash Transactions'} {where.lower()} {n}"
        if section == "trades":
            detail = _get(row, "levelofdetail").upper()
            if detail and detail != "EXECUTION":            # ORDER / SYMBOL_SUMMARY rows repeat executions
                skipped_detail += 1
                continue
            category = _get(row, "assetcategory", "assetclass").upper() or "STK"
            if category not in REPLAYED_CATEGORIES:
                skipped_categories += 1
                continue
            day = _date(_get(row, "tradedate", "datetime", "date"))
            symbol = _get(row, "symbol").upper()
            qty, price = _number(_get(row, "quantity")), _number(_get(row, "tradeprice", "price"))
            currency = _get(row, "currency", "currencyprimary").upper()
            side_text = _get(row, "buysell").upper()
            problems = []
            if day is None:
                problems.append("the trade date is missing or not a date")
            elif day > today:
                problems.append("the trade date is in the future")
            if not symbol:
                problems.append("the symbol is missing")
            elif not SYMBOL.fullmatch(symbol):
                problems.append("the symbol is not a valid symbol")
            if qty is None or qty == 0:
                problems.append("the quantity is missing or zero")
            if price is None or price <= 0:
                problems.append("the price is missing or not a positive number")
            if qty is not None and price is not None and abs(qty) * price > MAX_VALUE:
                problems.append("the trade value is unrealistically large")
            if not currency:
                problems.append("the currency is missing")
            elif not CURRENCY.fullmatch(currency):
                problems.append("the currency is not a 3-letter code")
            if side_text.startswith("BUY"):
                side = "buy"
            elif side_text.startswith("SELL"):
                side = "sell"
            elif not side_text and qty:
                side = "buy" if qty > 0 else "sell"
            else:
                side = None
                problems.append("Buy/Sell is not BUY or SELL")
            if problems:
                errors.append(f"{label}: " + "; ".join(problems) + ".")
                continue
            q = abs(qty)
            net = _number(_get(row, "netcash"))
            if net is None:
                proceeds, commission = _number(_get(row, "proceeds")), _number(_get(row, "ibcommission")) or 0.0
                gross = proceeds if proceeds is not None else (q * price if side == "sell" else -q * price)
                net = gross + commission
            if not np.isfinite(net) or abs(net) > MAX_VALUE:
                errors.append(f"{label}: the cash amount is unrealistically large.")
                continue
            trades.append(IbkrTrade(trade_date=day, symbol=symbol, side=side, quantity=q, price=price,
                                    currency=currency, asset_category=category, cash_change=net))
        else:
            day = _date(_get(row, "datetime", "date", "settledate", "reportdate"))
            amount = _number(_get(row, "amount"))
            currency = _get(row, "currency", "currencyprimary").upper()
            symbol = _get(row, "symbol").upper() or None
            problems = []
            if day is None:
                problems.append("the date is missing or not a date")
            elif day > today:
                problems.append("the date is in the future")
            if amount is None:
                problems.append("the amount is missing or not a number")
            elif abs(amount) > MAX_VALUE:
                problems.append("the amount is unrealistically large")
            if not currency:
                problems.append("the currency is missing")
            elif not CURRENCY.fullmatch(currency):
                problems.append("the currency is not a 3-letter code")
            if symbol is not None and not SYMBOL.fullmatch(symbol):
                problems.append("the symbol is not a valid symbol")
            if problems:
                errors.append(f"{label}: " + "; ".join(problems) + ".")
                continue
            if amount == 0:
                continue
            cash.append(IbkrCashTransaction(day=day, kind=_cash_kind(_get(row, "type"), amount), amount=amount,
                                            currency=currency, symbol=symbol))

    if errors:
        shown = errors[:10]
        more = f" (and {len(errors) - 10} more)" if len(errors) > 10 else ""
        raise IbkrReportError("The report has rows that cannot be read: " + " ".join(shown) + more)
    if not trades:
        raise IbkrReportError("The report has no stock, ETF, bond or crypto trades in its period. "
                              "Choose a longer period in your Flex Query.")
    currencies = sorted({t.currency for t in trades})
    if len(currencies) > 1:
        raise IbkrReportError(f"The trades are in {len(currencies)} currencies; only one trading currency "
                              "per report is supported for now.")
    currency = currencies[0]

    notes: list[str] = []
    if skipped_categories:
        notes.append(f"{skipped_categories} trades in options, futures or currency conversions were left out.")
    if skipped_detail:
        notes.append(f"{skipped_detail} order or summary rows were skipped (each execution is counted once).")
    other_ccy = [c for c in cash if c.currency != currency]
    if other_ccy:
        notes.append(f"{len(other_ccy)} cash transactions in other currencies than {currency} were left out.")
        cash = [c for c in cash if c.currency == currency]
    trades.sort(key=lambda t: t.trade_date)
    cash.sort(key=lambda c: c.day)
    days = [t.trade_date for t in trades] + [c.day for c in cash]
    return IbkrReport(format=fmt, currency=currency, trades=trades, cash_transactions=cash, start=min(days),
                      end=max(days), notes=notes)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------
def _business_day(d: date) -> pd.Timestamp:
    """Weekend events (crypto, late bookings) count on the next business day."""
    ts = pd.Timestamp(d)
    return ts if ts.dayofweek < 5 else ts + pd.offsets.BDay(1)


def _classes(symbols: Iterable[str], report: IbkrReport, given: Mapping[str, str],
             notes: list[str]) -> dict[str, str]:
    category = {t.symbol: t.asset_category for t in report.trades}
    out, assumed = {}, []
    for s in symbols:
        c = given.get(s)
        if c in ASSET_CLASSES and c != "cash":
            out[s] = c
        else:
            out[s] = REPLAYED_CATEGORIES.get(category.get(s, "STK"), "equity")
            assumed.append(s)
    if assumed:
        notes.append(f"Asset class taken from IBKR's category for: {', '.join(sorted(assumed))}.")
    return out


def to_trading_history(report: IbkrReport, *, prices: pd.DataFrame, asset_classes: Mapping[str, str],
                       as_of: date) -> IbkrHistory:
    """Replay the report day by day (business days from the first event to as_of) into a TradingHistory.

    prices: daily closes, index = dates, columns = symbols (missing columns or days are allowed: those days are
    valued at the last trade price, and labeled). asset_classes: symbol -> equity/bond/commodity/crypto (Agent C's
    catalog); missing symbols take IBKR's category. Events after as_of are left out.
    """
    notes = list(report.notes)
    end = pd.Timestamp(as_of)
    trades = [t for t in report.trades if _business_day(t.trade_date) <= end]
    flows_in = [c for c in report.cash_transactions if _business_day(c.day) <= end]
    late = len(report.trades) - len(trades) + len(report.cash_transactions) - len(flows_in)
    if late:
        notes.append(f"{late} transactions after {as_of.isoformat()} were left out.")
    if not trades:
        raise IbkrReportError("The report has no trades on or before the analysis date.")

    start = min([_business_day(t.trade_date) for t in trades] + [_business_day(c.day) for c in flows_in])
    days = pd.bdate_range(start, end)
    symbols = sorted({t.symbol for t in trades})
    pos_of = {d: i for i, d in enumerate(days)}

    # Quantities: daily changes, then running totals.
    dq = np.zeros((len(days), len(symbols)))
    trade_px = pd.DataFrame(np.nan, index=days, columns=symbols)
    cash_delta = np.zeros(len(days))
    flow_delta = np.zeros(len(days))
    for t in trades:
        i, j = pos_of[_business_day(t.trade_date)], symbols.index(t.symbol)
        dq[i, j] += t.quantity if t.side == "buy" else -t.quantity
        trade_px.iat[i, j] = t.price
        cash_delta[i] += t.cash_change
    for c in flows_in:
        i = pos_of[_business_day(c.day)]
        cash_delta[i] += c.amount
        if c.kind in ("deposit", "withdrawal"):
            flow_delta[i] += c.amount
    qty = np.cumsum(dq, axis=0)

    opening_qty = np.clip(-qty.min(axis=0), 0.0, None)
    opening_qty[opening_qty < TOL] = 0.0
    if opening_qty.any():
        qty = qty + opening_qty
        held = [s for s, q in zip(symbols, opening_qty) if q > 0]
        notes.append(f"Shares sold before any purchase in the report were assumed held from the start: "
                     f"{', '.join(held)}.")
    qty[np.abs(qty) < TOL] = 0.0

    # Prices: market closes where given (carried forward), else the last trade price (labeled).
    market = prices.copy() if prices is not None else pd.DataFrame()
    market.index = pd.DatetimeIndex(market.index).normalize()
    market = market.reindex(columns=symbols).astype(float).where(lambda df: df > 0)
    market = market.reindex(market.index.union(days)).sort_index().ffill().reindex(days)
    fallback = trade_px.ffill().bfill()
    used = market.combine_first(fallback)
    from_trades = market.isna() & (qty > 0)
    valued_at_trade = [s for s in symbols if from_trades[s].any()]
    if valued_at_trade:
        parts = [f"{s} ({int(from_trades[s].sum())} days)" for s in valued_at_trade]
        notes.append("No market price for some days; those days were valued at the last trade price: "
                     + ", ".join(parts) + ".")
    values = qty * used.to_numpy()

    # Cash: trades, dividends, fees and flows. Money spent before any deposit was there at the start.
    cash = np.cumsum(cash_delta)
    opening_cash = max(0.0, -float(cash.min()))
    if opening_cash > 0.01:
        notes.append(f"Purchases came before enough deposits in the report, so {opening_cash:,.2f} {report.currency} "
                     "was assumed to be in the account at the start.")
    cash = np.clip(cash + opening_cash, 0.0, None)
    opening_value = float(np.dot(opening_qty, used.iloc[0].to_numpy())) + opening_cash
    flow_delta[0] += opening_value                         # day-one money: never counted as a return

    positions = pd.DataFrame(values, index=days, columns=symbols)
    positions[CASH_ID] = cash
    flows = pd.Series(flow_delta, index=days)
    flows = flows[np.abs(flows) > TOL]
    classes = _classes(symbols, report, asset_classes, notes)
    history = TradingHistory(
        positions=positions, external_flows=flows,
        trades=[Trade(trade_date=_business_day(t.trade_date).date(), asset_id=t.symbol, side=t.side,
                      value=t.quantity * t.price) for t in trades],
        asset_classes={**classes, CASH_ID: "cash"}, asset_prices=used[symbols], base_currency=report.currency)

    last = positions.iloc[-1]
    total = float(last.sum())
    weights = {k: float(v / total) for k, v in last.items() if v > TOL} if total > TOL else {}
    if not weights:
        notes.append("The account was empty at the end of the period.")
    if len(days) < MIN_HISTORY_DAYS:
        notes.append(f"The report covers {len(days)} trading days; about {MIN_HISTORY_DAYS} (6 months) are needed "
                     "before your trading history can shape the portfolio.")
    return IbkrHistory(history=history, final_weights=weights, final_value=total, currency=report.currency,
                       start=days[0].date(), end=days[-1].date(), n_trades=len(trades),
                       valued_at_trade_price=valued_at_trade, notes=notes)


__all__ = ["CASH_ID", "IbkrCashTransaction", "IbkrHistory", "IbkrReport", "IbkrReportError", "IbkrTrade",
           "parse_ibkr_report", "spreadsheet_safe", "to_trading_history"]
