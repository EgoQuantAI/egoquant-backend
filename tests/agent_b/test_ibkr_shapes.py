"""IBKR input fuzz on the three real shapes: the Flex XML sample, the Flex CSV sample, and a Flex-token fetch (mocked
IBKR web service) that returns a report. Whatever the text, the parser raises nothing but IbkrReportError, the replay
raises nothing but IbkrReportError, the web loader raises nothing but HTTPException 422, and no value from the file
appears in an error message. Includes an extremely long symbol and negative prices and quantities.

Run with:  pytest -v tests/agent_b/test_ibkr_shapes.py      (no network, seeded)
"""

from __future__ import annotations

import random
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from egoquant.accounts.ibkr_input import IbkrFlexIn, load_ibkr_report
from egoquant.agents.behavioral import ibkr_flex
from egoquant.agents.behavioral.ibkr import (MAX_BYTES, IbkrReportError, parse_ibkr_report, to_trading_history)
from egoquant.agents.behavioral.ibkr_flex import BASE_URL, FlexError, fetch_flex_report
from tests.agent_b.test_ibkr_flex import QUERY, REF, TOKEN, Clock, FakeSession, ok_send

DATA = Path(__file__).parent / "data"
TODAY = date(2026, 10, 19)
AS_OF = date(2026, 9, 30)
XML = (DATA / "ibkr_cautious_seller.xml").read_text(encoding="utf-8")
CSV = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
CLASSES = {"VTI": "equity", "AGG": "bond"}
DAYS = pd.bdate_range("2025-01-01", "2026-09-30")
PRICES = {
    "none": pd.DataFrame(),
    "full": pd.DataFrame({"VTI": np.linspace(240, 290, len(DAYS)), "AGG": np.linspace(98, 101, len(DAYS))}, index=DAYS),
}


def flex_token_report(report: str) -> str:
    """The Flex Web Service path: SendRequest -> GetStatement, both answered by a fake IBKR; returns the report text."""
    clock = Clock()
    session = FakeSession([ok_send(), report], clock)
    return fetch_flex_report(TOKEN, QUERY, session=session, clock=clock, sleep=clock.sleep)


SHAPES = {"xml": XML, "csv": CSV, "flex_token_mock": flex_token_report(XML)}
FILENAME = {"xml": "report.xml", "csv": "report.csv", "flex_token_mock": None}


def survives(text: str, *, filename: str | None = None, label: str = "") -> str:
    """Run every stage on `text`. Returns "parsed" or "refused"; any other exception fails the test."""
    try:
        report = parse_ibkr_report(text, today=TODAY, filename=filename)
    except IbkrReportError as err:
        assert_no_value_echoed(str(err), text, label)
        return "refused"
    for prices in PRICES.values():                                     # the replay is part of the upload path
        try:
            history = to_trading_history(report, prices=prices, asset_classes=CLASSES, as_of=AS_OF)
        except IbkrReportError as err:
            assert_no_value_echoed(str(err), text, label)
        else:                                                          # what Agent B and Agent A get is always finite
            assert np.isfinite(history.history.positions.to_numpy()).all(), f"{label}: non-finite positions"
            assert np.isfinite(history.final_value), f"{label}: non-finite account value"
            assert all(np.isfinite(w) for w in history.final_weights.values()), f"{label}: non-finite weights"
    return "parsed"


def assert_no_value_echoed(message: str, text: str, label: str) -> None:
    """An error message may quote row numbers, never a value out of the file."""
    for token in re.findall(r"[A-Za-z0-9._\-]{6,}", text):
        if token.lower() in {"trade", "trades", "symbol", "quantity", "currency", "execution", "deposits"}:
            continue
        if re.fullmatch(r"[A-Za-z]+", token) and token.lower() in message.lower() and len(token) < 12:
            continue                                                    # a plain word that is also in a sentence
        assert token not in message, f"{label}: the error message quotes {token[:30]!r}"


def replace_first(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------
# The three shapes parse as shipped
# --------------------------------------------------------------------------
@pytest.mark.parametrize("shape", SHAPES)
def test_the_shipped_samples_parse(shape):
    report = parse_ibkr_report(SHAPES[shape], today=TODAY, filename=FILENAME[shape])
    assert len(report.trades) >= 4 and report.currency == "USD"
    assert survives(SHAPES[shape], filename=FILENAME[shape]) == "parsed"


def test_the_flex_mock_returns_the_report_unchanged():
    assert SHAPES["flex_token_mock"] == XML


# --------------------------------------------------------------------------
# Requested: an extremely long symbol, a negative price or quantity
# --------------------------------------------------------------------------
SYMBOL = {"xml": 'symbol="VTI"', "csv": '"VTI"', "flex_token_mock": 'symbol="VTI"'}
PRICE = {"xml": 'tradePrice="250"', "csv": '"300","250"', "flex_token_mock": 'tradePrice="250"'}
QTY = {"xml": 'quantity="300"', "csv": '"300","250"', "flex_token_mock": 'quantity="300"'}


def with_symbol(shape: str, symbol: str) -> str:
    return SHAPES[shape].replace(SYMBOL[shape], SYMBOL[shape].replace("VTI", symbol), 1)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("length", [25, 100, 1_000, 100_000, 140_000, 1_500_000])
def test_an_extremely_long_symbol_is_refused_with_ibkr_report_error(shape, length):
    text = with_symbol(shape, "A" * length)
    assert survives(text, filename=FILENAME[shape], label=f"{shape}/{length}") == "refused"
    with pytest.raises(IbkrReportError) as err:
        parse_ibkr_report(text, today=TODAY, filename=FILENAME[shape])
    assert "A" * 30 not in str(err.value) and len(str(err.value)) < 1_000


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("symbol", ["ＶＴＩ", "V​T​I", "VTI‮", "🚀🚀", "V\tTI", "VTI;DROP", "../../etc", "<VTI>", "VTI\x07",
                                    "﻿VTI", "VT" + "́" * 50])
def test_odd_symbols_never_escape_as_anything_else(shape, symbol):
    text = with_symbol(shape, symbol.replace('"', "'")).replace("&", "&amp;") if shape != "csv" else with_symbol(shape, symbol)
    survives(text, filename=FILENAME[shape], label=f"{shape}/{symbol!r}")


def with_price(shape: str, value: str) -> str:
    if shape == "csv":
        return CSV.replace('"300","250"', f'"300","{value}"', 1)
    return SHAPES[shape].replace('tradePrice="250"', f'tradePrice="{value}"', 1)


def with_quantity(shape: str, value: str) -> str:
    if shape == "csv":
        return CSV.replace('"300","250"', f'"{value}","250"', 1)
    return SHAPES[shape].replace('quantity="300"', f'quantity="{value}"', 1)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("price", ["-250", "-0.01", "0", "-0", "-1e308", "-inf", "nan", "", "abc", "1e999", "1e400",
                                   "9" * 400, "-" + "9" * 400, "250 250", "2,50", "٢٥٠"])
def test_a_negative_zero_or_absurd_price_is_refused_or_survived(shape, price):
    text = with_price(shape, price)
    outcome = survives(text, filename=FILENAME[shape], label=f"{shape}/price {price[:20]}")
    if price.strip().startswith("-") or price in {"0", "", "abc", "nan", "-inf", "1e999", "1e400"}:
        assert outcome == "refused", f"price {price[:20]!r} must be refused, not accepted"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("quantity", ["-300", "-0.000001", "0", "-0", "1e-300", "-1e308", "1e308", "1e999", "nan",
                                      "", "abc", "9" * 400, "-" + "9" * 400, "3 00"])
def test_a_negative_zero_or_absurd_quantity_never_escapes(shape, quantity):
    text = with_quantity(shape, quantity)
    outcome = survives(text, filename=FILENAME[shape], label=f"{shape}/quantity {quantity[:20]}")
    if quantity in {"0", "-0", "", "abc", "nan", "1e999"}:
        assert outcome == "refused"


@pytest.mark.parametrize("shape", SHAPES)
def test_a_negative_price_and_a_negative_quantity_together(shape):
    text = with_quantity(shape, "-300").replace('tradePrice="250"', 'tradePrice="-250"', 1) if shape != "csv" else \
        CSV.replace('"300","250"', '"-300","-250"', 1)
    assert survives(text, filename=FILENAME[shape]) == "refused"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("quantity", ["1e308", "9e307", "1e300", "1e200", "1e15", "1e13"])
def test_an_unrealistic_quantity_is_refused_so_the_replay_never_sees_infinity(shape, quantity):
    """Regression: 1e308 shares used to parse, then the replay produced inf account values and NaN weights."""
    with pytest.raises(IbkrReportError, match="unrealistically large"):
        parse_ibkr_report(with_quantity(shape, quantity), today=TODAY, filename=FILENAME[shape])


def test_an_unrealistic_cash_amount_or_net_cash_is_refused():
    huge_cash = XML.replace('amount="100000"', 'amount="1e300"', 1)
    with pytest.raises(IbkrReportError, match="unrealistically large"):
        parse_ibkr_report(huge_cash, today=TODAY)
    huge_net = XML.replace('netCash="-75001"', 'netCash="-1e300"', 1)
    with pytest.raises(IbkrReportError, match="unrealistically large"):
        parse_ibkr_report(huge_net, today=TODAY)
    assert parse_ibkr_report(with_quantity("xml", "1e9"), today=TODAY).trades[0].quantity == 1e9      # large but real


def test_a_buy_with_a_negative_quantity_is_read_as_a_buy_of_the_absolute_size():
    """IBKR writes buys positive and sells negative; the parser takes the BUY/SELL word and the size. Pinned so a
    change is a decision, not an accident."""
    report = parse_ibkr_report(with_quantity("xml", "-300"), today=TODAY)
    first = report.trades[0]
    assert (first.side, first.quantity) == ("buy", 300.0)


def test_a_negative_price_message_names_the_row_and_the_problem_not_the_value():
    with pytest.raises(IbkrReportError) as err:
        parse_ibkr_report(with_price("xml", "-250"), today=TODAY)
    assert "not a positive number" in str(err.value) and "-250" not in str(err.value) and "row 1" in str(err.value)


# --------------------------------------------------------------------------
# Structure and size
# --------------------------------------------------------------------------
STRUCTURE = {
    "nested_deep_xml": "<?xml version='1.0'?>" + "<a>" * 60_000 + "</a>" * 60_000,
    "nested_deep_xml_unclosed": "<FlexQueryResponse>" + "<a>" * 100_000,
    "attribute_bomb": '<FlexQueryResponse><FlexStatements><FlexStatement><Trades>' + "<Trade " + 'a="1" ' * 1 + "/>" * 1
                      + "</Trades></FlexStatement></FlexStatements></FlexQueryResponse>",
    "billion_laughs": '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><FlexQueryResponse>&b;</FlexQueryResponse>',
    "xxe": '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><FlexQueryResponse>&x;</FlexQueryResponse>',
    "external_dtd": '<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://example.invalid/x.dtd"><FlexQueryResponse/>',
    "cdata_trade": "<FlexQueryResponse><![CDATA[<Trade/>]]></FlexQueryResponse>",
    "xml_not_flex": "<html><body>hello</body></html>",
    "status_reply": '<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode></FlexStatementResponse>',
    "csv_huge_field": 'Symbol,Quantity,TradePrice\n"' + "x" * 200_000 + '",1,1\n',
    "csv_huge_field_unquoted": "Symbol,Quantity,TradePrice,Currency\n" + "Y" * 200_000 + ",1,1,USD\n",
    "csv_unbalanced_quote": 'Symbol,Quantity,TradePrice\n"VTI,1,1\nAGG,2,2\n',
    "csv_wrong_delimiter": "Symbol;Quantity;TradePrice\nVTI;1;1\n",
    "csv_only_header": "Symbol,Quantity,TradePrice,TradeDate,Buy/Sell,Currency\n",
    "csv_binary_noise": "Symbol,Quantity,TradePrice\n" + "".join(chr(i) for i in range(1, 32) if i not in (10, 13)) + ",1,1\n",
    "csv_nul": "Symbol,Quantity,TradePrice\nVTI,1,1\x00\n",
    "csv_cr_only": CSV.replace("\n", "\r"),
    "csv_crlf": CSV.replace("\n", "\r\n"),
    "csv_bom": "﻿" + CSV,
    "xml_bom": "﻿" + XML,
    "just_whitespace": " \n\t ",
    "single_char": "<",
    "single_quote": '"',
    "emoji_only": "🚀" * 1000,
    "a_pdf_pasted_as_text": "%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n",
    "a_zip_pasted_as_text": "PK\x03\x04\x14\x00\x06\x00",
}


@pytest.mark.parametrize("name", list(STRUCTURE))
def test_structural_attacks_never_escape(name):
    text = STRUCTURE[name]
    for filename in (None, "x.xml", "x.csv"):
        survives(text, filename=filename, label=name)
    with pytest.raises(HTTPException) as err:                         # the web path turns a refusal into a 422
        try:
            load_ibkr_report(text if name not in {"csv_crlf", "csv_bom", "xml_bom", "csv_cr_only"} else "<x/>", None)
        except HTTPException as exc:
            assert exc.status_code == 422
            raise
    assert err.value.status_code == 422


def test_too_many_rows_and_too_many_bytes():
    rows = XML.split("<Trades>")[0] + "<Trades>" + "<Trade symbol='VTI' quantity='1' tradePrice='1' currency='USD' " \
        "tradeDate='20250103' buySell='BUY'/>" * 20_001 + "</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
    with pytest.raises(IbkrReportError, match="more than"):
        parse_ibkr_report(rows, today=TODAY)
    with pytest.raises(IbkrReportError, match="2 MB"):
        parse_ibkr_report("x" * (MAX_BYTES + 1), today=TODAY)
    with pytest.raises(IbkrReportError, match="2 MB"):
        parse_ibkr_report(b"x" * (MAX_BYTES + 1), today=TODAY)


# --------------------------------------------------------------------------
# Seeded random mutations of each shape (what a damaged download or a hostile edit looks like)
# --------------------------------------------------------------------------
def mutate(text: str, rng: random.Random) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, 6)):
        op = rng.choice(["delete", "insert", "replace", "dup", "cut", "swap"])
        i = rng.randrange(max(1, len(chars)))
        junk = rng.choice(["", " ", "-", "--", "0", "1e999", "nan", "\u0000", "\n", '"', "'", "<", ">", "&", ",", ";",
                           "9" * rng.randint(1, 50), "A" * rng.randint(1, 3000), "é", "漢", "‮", "\x07", "%s", "{}"])
        if op == "delete":
            del chars[i:i + rng.randint(1, 40)]
        elif op == "insert":
            chars[i:i] = list(junk)
        elif op == "replace" and chars:
            chars[i:i + rng.randint(1, 10)] = list(junk)
        elif op == "dup" and chars:
            chars[i:i] = chars[max(0, i - 200):i]
        elif op == "cut" and chars:
            del chars[i:]
        elif op == "swap" and len(chars) > 3:
            j = rng.randrange(len(chars))
            chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


@pytest.mark.parametrize("shape", SHAPES)
def test_random_mutations_of_each_shape_only_ever_raise_ibkr_report_error(shape):
    rng = random.Random(20261019)
    outcomes = {"parsed": 0, "refused": 0}
    for n in range(250):
        text = mutate(SHAPES[shape], rng)
        outcomes[survives(text, filename=FILENAME[shape] if n % 2 else None, label=f"{shape}#{n}")] += 1
    assert outcomes["refused"] > 20                                   # the mutations do bite
    assert outcomes["parsed"] > 0                                     # and some mutated files are still fine


@pytest.mark.parametrize("shape", SHAPES)
def test_truncation_at_every_80th_character(shape):
    for cut in range(0, len(SHAPES[shape]), 80):
        survives(SHAPES[shape][:cut], filename=FILENAME[shape], label=f"{shape}[:{cut}]")


# --------------------------------------------------------------------------
# The Flex token path: whatever IBKR (or someone in the middle) answers, only FlexError comes out, and the report it
# hands over goes through the same parser
# --------------------------------------------------------------------------
GARBAGE = ["", " ", "<", "null", "{}", "<FlexStatementResponse>", "<FlexStatementResponse><Status>Success</Status>",
           ok_send().replace(REF, "x" * 5000), ok_send().replace(REF, "-1"), ok_send().replace(REF, "1e999"),
           ok_send(url="http://evil.example/steal"), ok_send(url="https://evil.example/GetStatement"),
           ok_send(url="https://ndcdyn.interactivebrokers.com.evil.example/x"), ok_send(url="\x00"), "\x00" * 10,
           "<FlexStatementResponse><Status>Fail</Status><ErrorCode>abc</ErrorCode></FlexStatementResponse>",
           "<FlexStatementResponse><Status>Fail</Status><ErrorCode>99999999999999999999</ErrorCode></FlexStatementResponse>",
           "🚀" * 100]


@pytest.mark.parametrize("first", GARBAGE)
@pytest.mark.parametrize("second", ["", "<", "garbage", XML[:50], "\x00", "🚀"])
def test_garbage_from_the_flex_service_only_raises_flex_error(first, second):
    clock = Clock()
    session = FakeSession([first, second] + [second] * 12, clock)
    try:
        text = fetch_flex_report(TOKEN, QUERY, session=session, clock=clock, sleep=clock.sleep)
    except FlexError as err:
        assert TOKEN not in str(err) and TOKEN not in repr(err)
        return
    survives(text, label="flex garbage")                              # a returned text is handed to the parser


@pytest.mark.parametrize("report", [XML, CSV, "", "<", "x" * 100, XML.replace("300", "-300"), XML.replace("250", "-250")])
def test_whatever_the_flex_service_returns_the_loader_gives_ok_or_a_422(report, monkeypatch):
    monkeypatch.setattr("egoquant.accounts.ibkr_input.fetch_flex_report", lambda token, query_id: report)
    flex = IbkrFlexIn(token=TOKEN, query_id=QUERY)
    try:
        loaded, source = load_ibkr_report(None, flex)
    except HTTPException as err:
        assert err.status_code == 422
        assert TOKEN not in str(err.detail)
    else:
        assert source == "ibkr_flex" and loaded.trades


def test_the_flex_token_goes_only_to_interactive_brokers_and_never_appears_in_an_error():
    """A hostile 'Url' in IBKR's answer is ignored: the statement is fetched from the real host, and when that
    answers with a failure the error carries no token."""
    fail_1015 = ("<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode>"
                 "<ErrorMessage>Invalid token</ErrorMessage></FlexStatementResponse>")
    clock = Clock()
    session = FakeSession([ok_send(url="https://evil.example/x"), fail_1015], clock)
    with pytest.raises(FlexError) as err:
        fetch_flex_report(TOKEN, QUERY, session=session, clock=clock, sleep=clock.sleep)
    assert all(call["url"].startswith(BASE_URL) for call in session.calls), [c["url"] for c in session.calls]
    assert TOKEN not in " ".join([str(err.value), repr(err.value), str(err.value.__cause__), str(err.value.__context__)])
