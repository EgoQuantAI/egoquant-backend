"""IBKR Flex trades report -> TradingHistory -> Agent B. All reports here are INVENTED, in IBKR's format."""

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from egoquant.agents.behavioral.core import time_weighted_returns
from egoquant.agents.behavioral.ibkr import (CASH_ID, IbkrReportError, parse_ibkr_report, spreadsheet_safe,
                                              to_trading_history)
from egoquant.agents.behavioral.models import BehavioralData
from tests.agent_b.test_behavioral import AS_OF, run_pair

DATA = Path(__file__).parent / "data"
TODAY = date(2026, 10, 18)
CLASSES = {"VTI": "equity", "AGG": "bond"}

# VTI path with two falls (about -15% and -14.5%) and recoveries; AGG nearly flat. Invented.
VTI_ANCHORS = [("2025-01-02", 250), ("2025-03-31", 268), ("2025-04-21", 228), ("2025-06-02", 262),
               ("2025-11-03", 290), ("2025-12-01", 248), ("2026-02-02", 286), ("2026-09-30", 305)]


def market_prices() -> pd.DataFrame:
    days = pd.bdate_range("2025-01-02", "2026-09-30")
    x = pd.DatetimeIndex([d for d, _ in VTI_ANCHORS]).as_unit("s").asi8.astype(float)
    vti = np.interp(days.as_unit("s").asi8.astype(float), x, [p for _, p in VTI_ANCHORS])
    agg = np.linspace(100.0, 102.0, len(days))
    return pd.DataFrame({"VTI": vti, "AGG": agg}, index=days)


def parse(name_or_text: str):
    path = DATA / name_or_text
    text = path.read_text(encoding="utf-8") if path.suffix in (".xml", ".csv") and path.exists() else name_or_text
    return parse_ibkr_report(text, today=TODAY)


def convert(report, prices=None):
    return to_trading_history(report, prices=market_prices() if prices is None else prices,
                              asset_classes=CLASSES, as_of=AS_OF)


def flex_xml(trades: str = "", cash: str = "", statements: int = 1) -> str:
    st = (f'<FlexStatement accountId="U1"><Trades>{trades}</Trades>'
          f'<CashTransactions>{cash}</CashTransactions></FlexStatement>') * statements
    return f'<FlexQueryResponse queryName="t" type="AF"><FlexStatements count="{statements}">{st}</FlexStatements></FlexQueryResponse>'


def trade(day="20250103", symbol="VTI", qty="10", price="250", side="BUY", ccy="USD", cat="STK", detail="EXECUTION",
          net=None):
    net_attr = f' netCash="{net}"' if net is not None else ""
    return (f'<Trade currency="{ccy}" assetCategory="{cat}" symbol="{symbol}" tradeDate="{day}" quantity="{qty}" '
            f'tradePrice="{price}" buySell="{side}" levelOfDetail="{detail}"{net_attr} />')


def deposit(day="20250102", amount="10000", kind="Deposits/Withdrawals", ccy="USD"):
    return f'<CashTransaction currency="{ccy}" dateTime="{day}" amount="{amount}" type="{kind}" />'


# --------------------------------------------------------------------------
# Parsing: both formats
# --------------------------------------------------------------------------
def test_xml_sample_is_read():
    r = parse("ibkr_cautious_seller.xml")
    assert r.format == "xml" and r.currency == "USD" and len(r.trades) == 6
    sells = [t for t in r.trades if t.side == "sell"]
    assert [(t.symbol, t.quantity, t.price) for t in sells] == [("VTI", 200, 232), ("VTI", 220, 252)]
    assert sells[0].cash_change == pytest.approx(46399)
    kinds = sorted(c.kind for c in r.cash_transactions)
    assert kinds == ["deposit", "deposit", "dividend", "other"]           # withholding tax is "other"
    assert r.start == date(2025, 1, 2) and r.end == date(2026, 2, 2)


def test_csv_sample_is_read():
    r = parse("ibkr_steady_holder.csv")
    assert r.format == "csv" and len(r.trades) == 4 and all(t.side == "buy" for t in r.trades)
    assert sum(c.amount for c in r.cash_transactions if c.kind == "deposit") == pytest.approx(108000)


def test_csv_with_ibkr_header_and_trailer_records():
    text = "\n".join([
        '"BOF","U1","sample","1","20250101","20260930"', '"BOA","U1"', '"BOS","TRNT","Trades"',
        '"HEADER","TRNT","CurrencyPrimary","Symbol","TradeDate","Quantity","TradePrice","Buy/Sell"',
        '"DATA","TRNT","USD","VTI","2025-01-03","10","250","BUY"',
        '"EOS","TRNT","1"', '"BOS","CTRN","Cash Transactions"',
        '"HEADER","CTRN","CurrencyPrimary","Date/Time","Amount","Type"',
        '"DATA","CTRN","USD","2025-01-02, 09:00:00","10000","Deposits/Withdrawals"',
        '"EOS","CTRN","1"', '"EOA","U1"', '"EOF","2"'])
    r = parse(text)
    assert len(r.trades) == 1 and r.trades[0].trade_date == date(2025, 1, 3)
    assert r.trades[0].cash_change == pytest.approx(-2500)                 # no NetCash column: quantity x price
    assert r.cash_transactions[0].day == date(2025, 1, 2)


def test_dates_in_ibkrs_formats():
    for day in ("20250103", "2025-01-03", "01/03/2025", "20250103;101500"):
        assert parse(flex_xml(trade(day=day))).trades[0].trade_date == date(2025, 1, 3)


def test_order_rows_options_and_fx_are_left_out_and_noted():
    r = parse(flex_xml(trade() + trade(detail="ORDER") + trade(symbol="VTI 250117C", cat="OPT")
                       + trade(symbol="EUR.USD", cat="CASH")))
    assert len(r.trades) == 1
    assert any("options, futures or currency" in n for n in r.notes)
    assert any("order or summary rows" in n for n in r.notes)


def test_dividends_are_cash_not_trades():
    r = parse(flex_xml(trade(), deposit() + deposit(amount="50", kind="Dividends")))
    assert len(r.trades) == 1 and {c.kind for c in r.cash_transactions} == {"deposit", "dividend"}


# --------------------------------------------------------------------------
# Rejections: user-safe messages, never a value from the file
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, words", [
    ("", "empty"),
    ("<html><body>hi</body></html>", "not an IBKR Flex Query report"),
    ('<FlexStatementResponse><Status>Fail</Status></FlexStatementResponse>', "Web Service reply"),
    ('<FlexQueryResponse><FlexStatements count="1"><FlexStatement/></FlexStatements></FlexQueryResponse>',
     "no Trades section"),
    ("<FlexQueryResponse><unclosed>", "not valid XML"),
    ('Statement,Header,Field Name,Field Value\nStatement,Data,BrokerName,Interactive Brokers', "Activity Statement"),
    ("date,type,ticker\n2025-01-02,buy,VTI", "no Trades section"),
    ('<!DOCTYPE x [<!ENTITY a "aaaa">]><FlexQueryResponse/>', "XML features"),
])
def test_anything_else_is_rejected(text, words):
    with pytest.raises(IbkrReportError, match=words):
        parse(text)


def test_two_accounts_are_rejected():
    with pytest.raises(IbkrReportError, match="exactly one account"):
        parse(flex_xml(trade(), statements=2))


def test_row_errors_name_row_and_field_but_never_the_value():
    bad = trade(price="-999.12345") + trade(day="20990101") + trade(side="SHORTX") + trade(symbol="", qty="0")
    with pytest.raises(IbkrReportError) as e:
        parse(flex_xml(trade() + bad))
    msg = str(e.value)
    assert "Trades row 2: the price is missing or not a positive number." in msg
    assert "Trades row 3: the trade date is in the future." in msg
    assert "Trades row 4: Buy/Sell is not BUY or SELL." in msg
    assert "Trades row 5" in msg and "symbol is missing" in msg and "quantity is missing or zero" in msg
    assert "999.12345" not in msg and "SHORTX" not in msg and "2099" not in msg


def test_account_id_never_reaches_the_models():
    r = parse("ibkr_cautious_seller.xml")
    assert "U0000000" not in r.model_dump_json()
    assert "U0000000" not in convert(r).model_dump_json(exclude={"history"})


def test_several_trading_currencies_are_rejected():
    with pytest.raises(IbkrReportError, match="2 currencies"):
        parse(flex_xml(trade() + trade(ccy="EUR")))


def test_cash_in_another_currency_is_left_out_with_a_note():
    r = parse(flex_xml(trade(), deposit() + deposit(ccy="EUR")))
    assert len(r.cash_transactions) == 1 and any("other currencies" in n for n in r.notes)


def test_size_and_no_trades_limits():
    with pytest.raises(IbkrReportError, match="larger than 2 MB"):
        parse_ibkr_report("x" * 2_000_001)
    with pytest.raises(IbkrReportError, match="no stock, ETF"):
        parse(flex_xml("", deposit()))


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------
def test_replay_values_holdings_and_cash():
    h = convert(parse("ibkr_cautious_seller.xml"))
    pos, px = h.history.positions, market_prices()
    day = pd.Timestamp("2025-03-03")
    assert pos.at[day, "VTI"] == pytest.approx(300 * px.at[day, "VTI"])
    assert pos.at[day, "AGG"] == pytest.approx(100 * px.at[day, "AGG"])
    assert pos.at[day, CASH_ID] == pytest.approx(100000 - 75001 - 10001)
    assert pos.at[pd.Timestamp("2025-03-25"), CASH_ID] == pytest.approx(100000 - 75001 - 10001 + 300 - 45)
    assert pos.at[pd.Timestamp("2025-04-17"), "VTI"] == pytest.approx(100 * px.at[pd.Timestamp("2025-04-17"), "VTI"])
    assert (pos >= 0).all().all() and not pos.isna().any().any()
    assert h.history.asset_classes == {"VTI": "equity", "AGG": "bond", CASH_ID: "cash"}
    assert h.n_trades == 6 and h.valued_at_trade_price == []


def test_a_sell_into_cash_keeps_the_account_value():
    h = convert(parse(flex_xml(trade(qty="10", price="250", net="-2500")
                               + trade(day="20250110", qty="-10", price="250", side="SELL", net="2500"),
                               deposit(amount="2500"))),
                prices=pd.DataFrame({"VTI": 250.0}, index=pd.bdate_range("2025-01-01", "2026-09-30")))
    total = h.history.positions.sum(axis=1)
    assert total.loc["2025-01-03":"2025-01-15"].round(6).nunique() == 1


def test_flows_are_not_returns():
    h = convert(parse("ibkr_steady_holder.csv"),
                prices=pd.DataFrame({"VTI": 250.0, "AGG": 100.0}, index=pd.bdate_range("2025-01-01", "2026-09-30")))
    r = time_weighted_returns(h.history.positions.sum(axis=1), h.history.external_flows)
    big = r.abs() > 0.01
    # Flat prices: the only moves are deposits, buys at other prices and fees/dividends, never a +5% "return".
    assert not big.loc["2025-07-01"] and not big.loc["2026-01-05"]
    assert set(h.history.external_flows.index.strftime("%Y-%m-%d")) >= {"2025-07-01", "2026-01-05"}


def test_missing_prices_use_the_last_trade_price_and_are_labeled():
    h = convert(parse("ibkr_cautious_seller.xml"), prices=market_prices().drop(columns=["AGG"]))
    assert h.valued_at_trade_price == ["AGG"]
    assert h.history.positions.at[pd.Timestamp("2025-06-02"), "AGG"] == pytest.approx(100 * 100)
    assert any("last trade price" in n and "AGG" in n for n in h.notes)


def test_shares_sold_before_any_purchase_were_held_from_the_start():
    h = convert(parse(flex_xml(trade(day="20250303", qty="-5", price="260", side="SELL", net="1300"))))
    assert (h.history.positions >= 0).all().all() and h.history.positions[CASH_ID].iloc[0] == pytest.approx(1300)
    assert h.history.external_flows.iloc[0] == pytest.approx(5 * market_prices().at[pd.Timestamp("2025-03-03"), "VTI"])
    assert any("assumed held from the start" in n for n in h.notes)


def test_buying_before_any_deposit_assumes_opening_cash():
    h = convert(parse(flex_xml(trade(qty="10", price="250", net="-2501"))))
    assert (h.history.positions[CASH_ID] >= 0).all()
    assert any("assumed to be in the account at the start" in n for n in h.notes)


def test_final_holdings_as_weights():
    h = convert(parse("ibkr_cautious_seller.xml"))
    assert sum(h.final_weights.values()) == pytest.approx(1.0)
    last = market_prices().iloc[-1]
    vti = 300 * last["VTI"]
    assert h.final_weights["VTI"] == pytest.approx(vti / h.final_value)
    assert set(h.final_weights) == {"VTI", "AGG", CASH_ID}


def test_events_after_as_of_are_left_out():
    r = parse(flex_xml(trade() + trade(day="20261015", qty="1")))
    h = to_trading_history(r, prices=market_prices(), asset_classes=CLASSES, as_of=date(2026, 9, 30))
    assert h.n_trades == 1 and any("after 2026-09-30" in n for n in h.notes)


def test_unknown_asset_class_comes_from_ibkr_category():
    h = to_trading_history(parse(flex_xml(trade(symbol="XYZ"))), prices=market_prices(), asset_classes={},
                           as_of=AS_OF)
    assert h.history.asset_classes["XYZ"] == "equity" and any("IBKR's category" in n for n in h.notes)


# --------------------------------------------------------------------------
# End to end with Agent B (tighten-only rules unchanged)
# --------------------------------------------------------------------------
def agent_b(name: str):
    h = convert(parse(name))
    data = {"ibkr-user": BehavioralData(investor_id="ibkr-user", history=h.history, data_source="real")}
    return run_pair(data, "ibkr-user")


def test_the_cautious_seller_is_tightened():
    result, report = agent_b("ibkr_cautious_seller.xml")
    assert report.status == "ok" and report.issues == []
    limit = next(c for c in report.constraints if c.type == "max_stress_loss")
    assert limit.source == "behavioral" and limit.hard                       # two consistent sales in falls
    assert report.threshold_source == "real" and 0.05 <= report.capitulation_threshold <= 0.15
    assert -result.final.stress[limit.scenario] <= limit.value + 1e-6     # Agent A meets the limit
    assert limit.value < 0.25                                                # below C4's own limit


def test_the_steady_holder_gets_no_limit():
    _, report = agent_b("ibkr_steady_holder.csv")
    assert report.status == "ok" and report.constraints == []
    assert next(f for f in report.features if f.name == "max_drawdown_held_through").value >= 0.08


# --------------------------------------------------------------------------
# Malicious and wrong files (B1): defusedxml, size, type, formula cells
# --------------------------------------------------------------------------
XXE = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
       '<FlexQueryResponse><FlexStatements count="1"><FlexStatement><Trades>'
       '<Trade currency="USD" symbol="&xxe;" tradeDate="20250103" quantity="1" tradePrice="1" buySell="BUY"/>'
       '</Trades></FlexStatement></FlexStatements></FlexQueryResponse>')
BILLION_LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                  + "".join(f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
                            for i in range(1, 10))
                  + ']><FlexQueryResponse>&lol9;</FlexQueryResponse>')
EXTERNAL_DTD = '<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://example.invalid/evil.dtd"><FlexQueryResponse/>'


@pytest.mark.parametrize("payload", [XXE, BILLION_LAUGHS, EXTERNAL_DTD], ids=["xxe", "billion_laughs", "external_dtd"])
def test_xml_attacks_are_refused_by_the_first_layer(payload):
    with pytest.raises(IbkrReportError, match="XML features a Flex report never uses"):
        parse(payload)


@pytest.mark.parametrize("payload", [XXE, BILLION_LAUGHS, EXTERNAL_DTD], ids=["xxe", "billion_laughs", "external_dtd"])
def test_xml_attacks_are_refused_by_defusedxml_alone(payload, monkeypatch):
    """Even with the first check switched off, defusedxml (DTDs forbidden) refuses the file: nothing is expanded."""
    from egoquant.agents.behavioral import ibkr
    monkeypatch.setattr(ibkr, "_has_dtd", lambda text: False)
    with pytest.raises(IbkrReportError, match="XML features a Flex report never uses"):
        parse(payload)


def test_xml_is_refused_not_parsed_unsafely_without_defusedxml(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_defusedxml(name, *args, **kwargs):
        if name.startswith("defusedxml"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_defusedxml)
    with pytest.raises(IbkrReportError, match="Upload the CSV version"):
        parse(flex_xml(trade()))


def test_the_parser_never_uses_xml_etree():
    source = (Path(__file__).parents[2] / "egoquant/agents/behavioral/ibkr.py").read_text(encoding="utf-8")
    import re
    assert not re.search(r"^\s*(import xml|from xml)", source, re.MULTILINE)       # no stdlib XML parser imported
    assert "from defusedxml import ElementTree" in source


def test_huge_files_are_refused_before_parsing():
    huge = flex_xml(trade()) + " " * 2_000_001
    with pytest.raises(IbkrReportError, match="larger than 2 MB"):
        parse_ibkr_report(huge)
    with pytest.raises(IbkrReportError, match="larger than 2 MB"):
        parse_ibkr_report(huge.encode())


def test_too_many_rows_are_refused():
    header = '"CurrencyPrimary","Symbol","TradeDate","Quantity","TradePrice","Buy/Sell"\n'
    rows = '"USD","VTI","20250103","1","250","BUY"\n' * 20_001
    with pytest.raises(IbkrReportError, match="more than 20000 rows"):
        parse(header + rows)


@pytest.mark.parametrize("raw, words", [
    (b"PK\x03\x04" + b"\x00" * 50, "ZIP or Excel"),
    (b"%PDF-1.7\n...", "PDF"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "old Excel"),
    (b"\xff\xfe<\x00F\x00", "not UTF-8"),
])
def test_binary_files_are_refused(raw, words):
    with pytest.raises(IbkrReportError, match=words):
        parse_ibkr_report(raw, today=TODAY)


def test_nul_bytes_are_refused():
    with pytest.raises(IbkrReportError, match="not a text file"):
        parse(flex_xml(trade()).replace("VTI", "V\x00TI"))


@pytest.mark.parametrize("filename, ok", [("report.xml", True), ("REPORT.XML", True), ("report.csv", False),
                                          ("report.xlsx", False), ("report.xml.exe", False), ("report", False)])
def test_extension_must_be_xml_or_csv_and_match_the_content(filename, ok):
    text = flex_xml(trade())
    if ok:
        assert parse_ibkr_report(text, today=TODAY, filename=filename).trades
    else:
        with pytest.raises(IbkrReportError, match="extension|Only .xml or .csv"):
            parse_ibkr_report(text, today=TODAY, filename=filename)


def test_csv_sample_with_its_filename():
    text = (DATA / "ibkr_steady_holder.csv").read_text(encoding="utf-8")
    assert parse_ibkr_report(text, today=TODAY, filename="ibkr_steady_holder.csv").format == "csv"


@pytest.mark.parametrize("cell", ['=HYPERLINK("http://evil.example","x")', "+cmd|' /C calc'!A0", "@SUM(1+1)",
                                  "-2+3+cmd|' /C calc'!A0"])
def test_formula_cells_never_pass_as_symbols(cell):
    csv_text = ('"CurrencyPrimary","Symbol","TradeDate","Quantity","TradePrice","Buy/Sell"\n'
                f'"USD","{cell.replace(chr(34), chr(34) * 2)}","20250103","1","250","BUY"\n')
    with pytest.raises(IbkrReportError) as e:
        parse(csv_text)
    assert "the symbol is not a valid symbol" in str(e.value) and cell not in str(e.value)


def test_formula_in_a_currency_or_an_ignored_column_never_reaches_the_models():
    with pytest.raises(IbkrReportError, match="currency is not a 3-letter code"):
        parse(flex_xml(trade(ccy="=1+1")))
    text = ('"CurrencyPrimary","Symbol","Description","TradeDate","Quantity","TradePrice","Buy/Sell"\n'
            '"USD","VTI","=HYPERLINK(""http://evil.example"")","20250103","1","250","BUY"\n')
    assert "HYPERLINK" not in parse(text).model_dump_json()           # descriptions are never kept


@pytest.mark.parametrize("value, expected", [("=SUM(A1)", "'=SUM(A1)"), ("+cmd", "'+cmd"), ("@x", "'@x"),
                                             ("-x", "'-x"), ("\tx", "'\tx"), ("VTI", "VTI"), ("-75001", "-75001"),
                                             ("+3.5", "+3.5"), ("", "")])
def test_spreadsheet_safe(value, expected):
    assert spreadsheet_safe(value) == expected
