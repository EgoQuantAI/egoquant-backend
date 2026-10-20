"""Twelve invented investors, one after another, through the app's own routes (offline, no AI, no network).

    python eval/twelve_users.py [--only KEY ...] [--out FILE.json] [--texts FILE.md] [--as-of YYYY-MM-DD]

For each investor: /profile (with and without the Risk Game) -> /preferences/form -> /portfolio (without a goal, with a goal,
with an IBKR history where they have one) -> /trade-check (one trade inside and one outside the category) -> What-If through the
engine -> (one investor replays the game and rebuilds). Every result is checked against the written rules and each check that
fails is a FINDING with the user, the step, what was expected and what happened. All investor-facing texts of a run are written
to a Markdown file so that they can be read as one person would read them.

The check rules (RULES below) are independent of the code that computes the category: the Risk Game blend of docs/RISK_GAME_BLEND.md
is recomputed here from the levels the profile reports. Prices come from the committed snapshot (EGOQUANT_PRICE_MODE=snapshot) and
the network is blocked, like in eval/demo_pack.py. No key is read.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["EGOQUANT_PRICE_MODE"] = "snapshot"          # before the app is imported: saved real prices, no live feed

_spec = importlib.util.spec_from_file_location("demo_pack_for_twelve_users", ROOT / "eval" / "demo_pack.py")
dp = importlib.util.module_from_spec(_spec)             # importing it sets the offline environment and blocks the network
_spec.loader.exec_module(dp)

from fastapi.testclient import TestClient  # noqa: E402

from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG  # noqa: E402
from egoquant.agents.market_intel.platforms import PLATFORMS, normalize_platform  # noqa: E402
from egoquant.agents.market_intel.snapshot import YAHOO_SNAPSHOT_PATH, SnapshotPriceSource  # noqa: E402
from egoquant.demo.personas import PERSONAS  # noqa: E402
from egoquant.preferences import load_preference_bank  # noqa: E402
from egoquant.risk_profile import load_question_bank  # noqa: E402

CATALOG = {i.ticker: i for i in DEFAULT_CATALOG}
GAME_DAY = "2026-10-16T09:00:00Z"
PRICES = SnapshotPriceSource(YAHOO_SNAPSHOT_PATH).prices

# The C-level table of egoquant/risk_profile/config.yaml, copied here on purpose: the check must not read the code it checks.
C_TABLE = {1: dict(r=1, equity=0.0, commodity=0.0, crypto=0.0), 2: dict(r=2, equity=0.0, commodity=0.0, crypto=0.0),
           3: dict(r=3, equity=0.40, commodity=0.10, crypto=0.0), 4: dict(r=4, equity=0.70, commodity=0.15, crypto=0.0),
           5: dict(r=5, equity=0.90, commodity=0.20, crypto=0.05)}
NAMES = {"VT": "VANGUARD TOTAL WORLD STOCK ETF", "QQQ": "INVESCO QQQ TRUST", "AGG": "ISHARES CORE US AGGREGATE",
         "BIL": "SPDR BLOOMBERG 1-3 MONTH T-BILL", "NVDA": "NVIDIA CORP", "IAU": "ISHARES GOLD TRUST", "SHY": "ISHARES 1-3 YEAR TREASURY BOND"}


# --------------------------------------------------------------------------------------------------------------------------
# Invented IBKR histories (real adjusted closes of the committed snapshot on each trade date)
# --------------------------------------------------------------------------------------------------------------------------
def _close(sym: str, day: str) -> float:
    series = PRICES[sym].dropna()
    return float(series.loc[:day].iloc[-1])


def flex_xml(title: str, deposits: list[tuple[str, int]], trades: list[tuple[str, str, str, float]]) -> str:
    rows, cash = [], []
    for day, amount in deposits:
        cash.append(f'<CashTransaction accountId="U0000000" currency="USD" assetCategory="" symbol="" '
                    f'description="CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" dateTime="{day}" amount="{amount}" '
                    f'type="Deposits/Withdrawals" />')
    for day, sym, side, dollars in trades:
        px = round(_close(sym, f"{day[:4]}-{day[4:6]}-{day[6:]}"), 2)
        qty = max(1, round(dollars / px))
        signed = qty if side == "BUY" else -qty
        proceeds = round(-signed * px, 2)
        rows.append(f'<Trade accountId="U0000000" currency="USD" assetCategory="STK" symbol="{sym}" description="{NAMES[sym]}" '
                    f'tradeDate="{day}" dateTime="{day};100512" quantity="{signed}" tradePrice="{px}" proceeds="{proceeds}" '
                    f'ibCommission="-1" netCash="{round(proceeds - 1, 2)}" buySell="{side}" levelOfDetail="EXECUTION" />')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<!-- INVENTED sample in IBKR's Flex Query XML format. Not real account data. {title} -->\n"
            '<FlexQueryResponse queryName="EgoQuant test" type="AF">\n<FlexStatements count="1">\n'
            '<FlexStatement accountId="U0000000" fromDate="20250101" toDate="20261015" period="Custom" '
            'whenGenerated="20261016;180000">\n<Trades>\n' + "\n".join(rows) + '\n</Trades>\n<CashTransactions>\n'
            + "\n".join(cash) + '\n</CashTransactions>\n</FlexStatement>\n</FlexStatements>\n</FlexQueryResponse>\n')


def holding_xml(invested: float) -> str:
    """The first purchase of someone without a history: a bond fund, a stock fund and bills, bought on one day."""
    return flex_xml("A first purchase.", [("20250102", int(invested))],
                    [("20250103", "AGG", "BUY", invested * 0.5), ("20250103", "VT", "BUY", invested * 0.35),
                     ("20250103", "BIL", "BUY", invested * 0.15)])


def panic_seller_xml() -> str:
    """100,000 put into stocks, then sold in every fall (the real falls of April 2025, March 2025, October 2025, June 2026)
    and bought back higher after the recovery. A lot of trades, on purpose."""
    trades = [("20250103", "VT", "BUY", 55000), ("20250103", "QQQ", "BUY", 25000), ("20250103", "NVDA", "BUY", 10000),
              ("20250103", "AGG", "BUY", 8000),
              ("20250310", "QQQ", "SELL", 15000), ("20250310", "NVDA", "SELL", 6000),
              ("20250404", "VT", "SELL", 25000), ("20250407", "VT", "SELL", 20000), ("20250407", "QQQ", "SELL", 8000),
              ("20250408", "VT", "SELL", 10000), ("20250408", "NVDA", "SELL", 4000),
              ("20250630", "VT", "BUY", 40000), ("20250707", "QQQ", "BUY", 18000), ("20250714", "NVDA", "BUY", 8000),
              ("20250910", "VT", "BUY", 14000),
              ("20251010", "VT", "SELL", 30000), ("20251010", "QQQ", "SELL", 14000), ("20251013", "NVDA", "SELL", 6000),
              ("20251215", "VT", "BUY", 36000), ("20251215", "QQQ", "BUY", 16000),
              ("20260323", "VT", "SELL", 22000), ("20260323", "QQQ", "SELL", 9000),
              ("20260420", "VT", "BUY", 24000), ("20260605", "VT", "SELL", 30000), ("20260605", "QQQ", "SELL", 14000),
              ("20260605", "NVDA", "SELL", 5000), ("20260615", "AGG", "BUY", 30000), ("20260917", "VT", "BUY", 22000)]
    return flex_xml("Panic seller: 100,000, sold in every fall and bought back higher.", [("20250102", 100000)], trades)


def calm_xml() -> str:
    """70,000 in a stock fund and a bond fund, a purchase on the first trading day of every month, nothing ever sold."""
    days = ["20250103", "20250203", "20250303", "20250401", "20250501", "20250602", "20250701", "20250801", "20250902", "20251001",
            "20251103", "20251201", "20260102", "20260202", "20260302", "20260401", "20260501", "20260601", "20260701", "20260803",
            "20260901", "20261001"]
    trades = [("20250103", "VT", "BUY", 40000), ("20250103", "AGG", "BUY", 25000)]
    for day in days[1:]:
        trades += [(day, "VT", "BUY", 700), (day, "AGG", "BUY", 350)]
    return flex_xml("Calm investor: 65,000 and a monthly purchase, nothing sold.", [("20250102", 65000)] + [(d, 1050) for d in days[1:]],
                    trades)


# --------------------------------------------------------------------------------------------------------------------------
# The twelve investors
# --------------------------------------------------------------------------------------------------------------------------
GL = {k: dict((c, v) for c, v in p.choices.items() if c.startswith("GL")) for k, p in PERSONAS.items()}
GL_LOW, GL_CAUTIOUS, GL_MID, GL_HIGH, GL_TOP = (GL["no_loss_saver"], GL["cautious_retiree"], GL["balanced_saver"],
                                                GL["growth_investor"], GL["crypto_trader"])


def user(key, who, gl, b, k, d="yes", k4=("stocks",), invested=10_000, needed=0, game=None, prefs=None, platforms=("Fidelity",),
         horizon=10, liquidity=0.0, min_cash=0.0, goal=None, monthly=0.0, ibkr=None, trades=(), what_ifs=(), later_game=None):
    b1, b2, b4, b5, b6 = b
    choices = {**gl, "B1": b1, "B2": b2, "B4": b4, "B5": b5, "B6": b6, **dict(zip(("K1", "K2", "K3", "K5"), k)), "D1": d}
    return dict(key=key, who=who, choices=choices, k4=list(k4), invested=invested, needed=needed, game=game, prefs=prefs or {},
                platforms=list(platforms), horizon=horizon, liquidity=liquidity, min_cash=min_cash, goal=goal, monthly=monthly,
                ibkr=ibkr, trades=list(trades), what_ifs=list(what_ifs), later_game=later_game)


KNOWS = ("more", "less", "false", "y3_5")                # the Big Three right, a few years of experience
KNOWS_NOT = ("dk", "dk", "dk", "none")

USERS = [
    user("student", "22-year-old student, 500 USD, no income, no savings, first investment", GL_HIGH,
         ("growth", "gt5", "lt3", "p25_50", "none"), KNOWS_NOT, k4=("none",), invested=500, needed=0,
         game=dict(tolerance=80, loss_acceptance=60), prefs={"P1": ["stocks"], "P3": ["us_stocks"], "P4": ["funds"]},
         platforms=("Robinhood",), horizon=10, goal=8000, monthly=50,
         trades=[dict(ticker="NVDA", side="buy", amount=300), dict(ticker="VT", side="buy", amount=200)],
         what_ifs=[dict(label="What if I add 100 a month?", extra_monthly_contribution=100)]),
    user("retiree3", "68-year-old retiree, 300,000, needs 240,000 back, goal in 3 years", GL_CAUTIOUS,
         ("retirement", "y1_3", "gt6", "p50_75", "stable"), KNOWS, k4=("deposits", "bonds"), invested=300_000, needed=240_000,
         game=dict(tolerance=20, loss_acceptance=40),
         prefs={"P1": ["bonds", "cash", "stocks"], "P6": ["government_bonds", "corporate_bonds"], "P7": ["funds"]},
         platforms=("Fidelity",), horizon=3, goal=320_000,
         trades=[dict(ticker="VT", side="buy", amount=30_000), dict(ticker="SHY", side="buy", amount=20_000)],
         what_ifs=[dict(label="What if I need 340,000?", goal_amount=340_000)]),
    user("needs1y", "45-year-old who needs the money in under a year (house), 80,000, 70,000 needed back, strong finances", GL_MID,
         ("house", "lt1", "gt6", "lt25", "stable"), KNOWS, k4=("stocks", "bonds"), invested=80_000, needed=70_000,
         game=dict(tolerance=70, loss_acceptance=70), prefs={"P1": ["stocks", "bonds", "cash"], "P4": ["funds"]},
         platforms=("Interactive Brokers",), horizon=1, goal=84_000,
         trades=[dict(ticker="VT", side="buy", amount=20_000), dict(ticker="BIL", side="buy", amount=10_000)],
         what_ifs=[dict(label="What if I wait 2 years?", horizon_years=2)]),
    user("irregular", "38-year-old freelancer, irregular income, 15 years, 150,000, savings over 6 months, small share of wealth", GL_HIGH,
         ("growth", "gt5", "gt6", "lt25", "unstable"), KNOWS, k4=("stocks", "bonds"), invested=150_000, needed=0,
         game=dict(tolerance=50, loss_acceptance=50), prefs={"P1": ["stocks", "bonds", "gold"], "P2": ["technology"], "P4": ["funds"]},
         platforms=("Charles Schwab",), horizon=15, goal=400_000, monthly=500,
         trades=[dict(ticker="VT", side="buy", amount=15_000), dict(ticker="IBIT", side="buy", amount=5_000)],
         what_ifs=[dict(label="What if I add 500 a month more?", extra_monthly_contribution=500)]),
    user("crypto_fan", "29-year-old crypto fan with a Binance and an IBKR account, bold in everything", GL_TOP,
         ("growth", "gt5", "gt6", "lt25", "stable"), KNOWS, k4=("stocks", "crypto", "derivatives"), invested=25_000, needed=0,
         game=dict(tolerance=90, loss_acceptance=80),
         prefs={"P1": ["stocks", "gold", "crypto", "altcoins"], "P2": ["technology"], "P4": ["individual"]},
         platforms=("Interactive Brokers", "Binance"), horizon=8, goal=60_000, monthly=200,
         trades=[dict(ticker="IBIT", side="buy", amount=3_000), dict(ticker="NVDA", side="buy", amount=2_000)],
         what_ifs=[dict(label="What if I drop all crypto?", exclude_assets=["crypto"])]),
    user("excluder", "41-year-old who wants healthcare and consumer companies but NOT technology, energy or financials", GL_HIGH,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=60_000, needed=10_000,
         game=dict(tolerance=60, loss_acceptance=60),
         prefs={"P1": ["stocks", "bonds"], "P2": ["healthcare", "consumer"], "P4": ["mix"],
                "P5": ["technology", "energy", "financials"]},
         platforms=("Charles Schwab",), horizon=12, goal=120_000, monthly=300,
         trades=[dict(ticker="NVDA", side="buy", amount=3_000), dict(ticker="VT", side="buy", amount=3_000)],
         what_ifs=[dict(label="What if I drop all bonds?", exclude_assets=["bond"])]),
    user("panic_ibkr", "52-year-old with a large IBKR history: sold in every fall, bought back higher", GL_HIGH,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=100_000, needed=20_000,
         game=dict(tolerance=70, loss_acceptance=70), prefs={"P1": ["stocks", "bonds"], "P4": ["funds"]},
         platforms=("Interactive Brokers",), horizon=10, goal=180_000, ibkr=panic_seller_xml(),
         trades=[dict(ticker="VT", side="sell", amount=20_000), dict(ticker="QQQ", side="buy", amount=15_000)],
         what_ifs=[dict(label="What if I add 300 a month?", extra_monthly_contribution=300)]),
    user("calm_ibkr", "47-year-old with a calm IBKR history: a monthly purchase, nothing ever sold", GL_MID,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=70_000, needed=10_000,
         game=dict(tolerance=50, loss_acceptance=50), prefs={"P1": ["stocks", "bonds"], "P4": ["funds"]},
         platforms=("Interactive Brokers",), horizon=15, goal=160_000, monthly=700, ibkr=calm_xml(),
         trades=[dict(ticker="VT", side="buy", amount=5_000), dict(ticker="VT", side="sell", amount=30_000)],
         what_ifs=[dict(label="What if I add 300 a month more?", extra_monthly_contribution=300)]),
    user("skips_game", "35-year-old who skips the Risk Game", GL_MID,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=40_000, needed=5_000,
         game=None, prefs={"P1": ["stocks", "bonds", "gold"], "P4": ["funds"]}, platforms=("Fidelity",), horizon=8, goal=70_000,
         trades=[dict(ticker="VT", side="buy", amount=4_000), dict(ticker="TLT", side="buy", amount=4_000)],
         what_ifs=[dict(label="What if I invest for 12 years?", horizon_years=12)]),
    user("bold_low_cap", "26-year-old who plays the game boldly but whose finances cap them: 12,000, 9,000 needed in 2 years", GL_HIGH,
         ("house", "y1_3", "lt3", "gt75", "none"), KNOWS, k4=("stocks",), invested=12_000, needed=9_000,
         game=dict(tolerance=100, loss_acceptance=100), prefs={"P1": ["stocks", "bonds", "cash"]},
         platforms=("Robinhood",), horizon=2, goal=13_000,
         trades=[dict(ticker="NVDA", side="buy", amount=2_000), dict(ticker="BIL", side="buy", amount=2_000)],
         what_ifs=[dict(label="What if I wait 4 years?", horizon_years=4)]),
    user("twenty_areas", "33-year-old who ticks every interest they are offered", GL_TOP,
         ("growth", "gt5", "gt6", "lt25", "stable"), KNOWS, k4=("stocks", "bonds", "crypto"), invested=90_000, needed=0,
         game=dict(tolerance=70, loss_acceptance=70),
         prefs={"P1": ["stocks", "bonds", "cash", "real_estate", "gold", "precious_metals", "commodities", "crypto", "altcoins"],
                "P2": ["technology", "healthcare", "financials", "energy", "consumer", "industrials", "utilities", "communication",
                       "materials"],
                "P3": ["us_stocks", "international_stocks", "emerging_markets", "china", "small_caps", "dividend_stocks"],
                "P4": ["mix"], "P6": ["government_bonds", "corporate_bonds", "inflation_protected", "long_term_bonds",
                                       "international_bonds"]},
         platforms=("Interactive Brokers", "Binance"), horizon=15, goal=200_000,
         trades=[dict(ticker="VT", side="buy", amount=5_000), dict(ticker="IBIT", side="buy", amount=2_000)],
         what_ifs=[dict(label="What if I add 400 a month?", extra_monthly_contribution=400)]),
    user("short_horizon", "50-year-old with strong finances who must start spending the money within a year", GL_HIGH,
         ("house", "lt1", "gt6", "lt25", "stable"), KNOWS, k4=("stocks", "bonds"), invested=100_000, needed=20_000,
         game=dict(tolerance=60, loss_acceptance=60), prefs={"P1": ["bonds", "cash"]}, platforms=("Fidelity",), horizon=1,
         goal=105_000,
         trades=[dict(ticker="BIL", side="buy", amount=5_000)],
         what_ifs=[dict(label="What if I wait 3 years?", horizon_years=3)]),
    user("no_stocks", "44-year-old growth-minded investor who ticks only bonds, cash and gold (no stocks)", GL_HIGH,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=70_000, needed=5_000,
         game=dict(tolerance=60, loss_acceptance=60), prefs={"P1": ["bonds", "cash", "gold"], "P6": ["government_bonds"]},
         platforms=("Charles Schwab",), horizon=10, goal=110_000, monthly=300,
         trades=[dict(ticker="AGG", side="buy", amount=3_000)],
         what_ifs=[dict(label="What if I add 300 a month?", extra_monthly_contribution=300)]),
    user("changes_game", "40-year-old who plays the game timidly, builds, then replays it boldly and rebuilds", GL_MID,
         ("growth", "gt5", "m3_6", "p25_50", "stable"), KNOWS, k4=("stocks", "bonds"), invested=55_000, needed=5_000,
         game=dict(tolerance=10, loss_acceptance=10), later_game=dict(tolerance=90, loss_acceptance=90),
         prefs={"P1": ["stocks", "bonds", "gold"]}, platforms=("Fidelity",), horizon=10, goal=100_000,
         trades=[dict(ticker="VT", side="buy", amount=5_000), dict(ticker="AGG", side="buy", amount=3_000)],
         what_ifs=[dict(label="What if I add 200 a month?", extra_monthly_contribution=200)]),
]


# --------------------------------------------------------------------------------------------------------------------------
# Rules (the expectations; independent of the scoring code)
# --------------------------------------------------------------------------------------------------------------------------
def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def expected_class(profile: dict, weight: float = 1 / 3, may_raise: bool = True) -> str:
    """docs/RISK_GAME_BLEND.md section 2, recomputed from the levels the profile reports."""
    if profile.get("no_loss_gate"):
        return "C1"
    t, cap = profile["tolerance_level"], profile["capacity_level"]
    if profile.get("game_played"):
        score = profile["game_score"]
        game_level = min(5, 1 + int(score // 20))
        blend = max(1, min(5, round_half_up((1 - weight) * t + weight * game_level)))
        return f"C{min(cap, blend) if may_raise else min(cap, t, blend)}"
    return f"C{min(cap, t)}"


class Findings:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, user_key: str, step: str, expected: str, actual: str, severity: str = "B") -> None:
        row = dict(id=f"T{len(self.rows) + 1:02d}", severity=severity, user=user_key, step=step, expected=expected, actual=actual)
        self.rows.append(row)


def all_strings(obj, path=""):
    """Every string in a JSON-like object with its path."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from all_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from all_strings(v, f"{path}[{i}]")


JARGON = re.compile(r"\b(CVaR|VaR|Sharpe|Sortino|covariance|Ledoit|Monte Carlo|bootstrap|eigen|shrinkage|solver|optimi[sz]er|"
                    r"R-level|r_level|max_r_level|binding_dimension|snapshot_first|infeasible|constraint|heuristic|HMAC|JSON|API|"
                    r"None|null|NaN|True|False)\b")


def text_of_report(rep: dict) -> list[tuple[str, str]]:
    skip = ("allocation", "menu", "stress", "trades", "fan")
    return [(p, s) for p, s in all_strings({k: v for k, v in rep.items() if k not in skip}) if len(s) > 12]


def check_holdings(f: Findings, u: dict, step: str, built: dict, profile: dict) -> None:
    """Products are allowed for the category and the platform; weights add up; exclusions and caps hold."""
    rep = built["report"]
    cls = int(profile["c_level"][1:])
    table = C_TABLE[cls]
    markets = set()
    for name in u["platforms"]:
        markets |= set(PLATFORMS[normalize_platform(name)].markets)
    weights = {a["asset_id"]: a["weight"] for a in rep.get("allocation") or []}
    total = sum(weights.values())
    if abs(total - 1.0) > 0.005:
        f.add(u["key"], step, "weights add up to 1", f"they add up to {total:.4f}", "A")
    blocked_types = set(profile.get("blocked_product_types") or [])
    excluded = set(u["prefs"].get("P5", [])) - {"none"}
    equity = commodity = crypto = 0.0
    for ticker, w in weights.items():
        inst = CATALOG.get(ticker)
        if inst is None:
            continue                                      # cash line or a name the catalog does not know
        if inst.r_level > table["r"]:
            f.add(u["key"], step, f"only products up to R{table['r']} in {profile['c_level']}",
                  f"{ticker} (R{inst.r_level}) is held at {w:.1%}", "A")
        if inst.market not in markets:
            f.add(u["key"], step, f"only products tradable on {u['platforms']}", f"{ticker} is a {inst.market} product", "A")
        if inst.product_type in blocked_types:
            f.add(u["key"], step, f"no {inst.product_type} (product_access={profile.get('product_access')})", f"{ticker} held", "A")
        if inst.sector and inst.sector in excluded and w > 1e-6:
            f.add(u["key"], step, f"the excluded sector {inst.sector} is not held", f"{ticker} ({inst.sector}) at {w:.1%}", "A")
        if inst.asset_class == "equity":
            equity += w
        elif inst.asset_class == "commodity":
            commodity += w
        elif inst.asset_class == "crypto":
            crypto += w
    if built["approved"]:
        for label, value, cap in (("stocks", equity, table["equity"]), ("commodities", commodity, table["commodity"]),
                                  ("crypto", crypto, table["crypto"])):
            if value > cap + 0.005:
                f.add(u["key"], step, f"{label} at most {cap:.0%} in {profile['c_level']}", f"{label} at {value:.1%}", "A")


def check_goal_wording(f: Findings, u: dict, step: str, built: dict) -> None:
    goal = (built["report"].get("goal") or {})
    chance, in_100 = goal.get("chance"), goal.get("in_100")
    if chance is None or in_100 is None:
        return
    shown = re.findall(r"\d+", str(chance))
    if shown and abs(int(shown[0]) - in_100) > 1 and "%" in str(chance):
        f.add(u["key"], step, f"'{in_100} in 100' matches the chance as shown ({chance})", f"chance {chance!r}, in_100 {in_100}", "A")
    for path, text in all_strings(goal):
        for m in re.finditer(r"(\d{1,3}) in 100", text):
            if int(m.group(1)) != in_100 and path.split(".")[0] in ("headline", "plan", "chance"):
                f.add(u["key"], step, f"'{in_100} in 100' everywhere", f"{path}: {text[:120]}", "A")


def check_html(f: Findings, u: dict, step: str, built: dict) -> None:
    """The numbers of the report are in its HTML page, and the page says 'hypothetical' / 'not a forecast'."""
    html, rep = built["html"], built["report"]
    plain = re.sub(r"<[^>]+>", " ", html)
    for label, value in (("stocks", rep.get("equity_weight")), ("volatility", rep.get("ex_ante_vol"))):
        if value is None:
            continue
        pcts = {f"{value * 100:.0f}%", f"{value * 100:.1f}%", f"{value:.1%}", f"{value:.0%}"}
        if not any(p in plain for p in pcts):
            f.add(u["key"], step, f"the {label} number ({value:.4f}) is in the HTML", "not found as a percentage in the page", "B")
    goal = rep.get("goal") or {}
    n = goal.get("in_100")
    if n in (0, 100):
        return                                              # "In every one of the N simulated futures": the documented wording
    if n is not None and not any(w.format(n=n) in plain for w in ("{n} of every 100", "{n} in 100", "{n} of 100")):
        f.add(u["key"], step, f"the HTML says '{n} of every 100'", "the page does not contain it", "B")
    low = plain.lower()
    if "hypothetical" not in low:
        f.add(u["key"], step, "the page says 'hypothetical' next to the stress numbers", "the word is not on the page", "A")
    if "forecast" not in low:
        f.add(u["key"], step, "the page says 'not a forecast'", "the word is not on the page", "A")


# --------------------------------------------------------------------------------------------------------------------------
# One investor
# --------------------------------------------------------------------------------------------------------------------------
def run_user(client, u: dict, f: Findings, texts: list[str]) -> dict:
    key = u["key"]
    bank_version = load_question_bank().questionnaire_version
    pref_version = load_preference_bank().preferences_version
    base = {"questionnaire_version": bank_version, "choices": u["choices"], "multi_choices": {"K4": u["k4"]},
            "amount_invested": u["invested"], "amount_needed": u["needed"]}
    game = ({**u["game"], "completed_at": GAME_DAY} if u["game"] else None)
    answers = {**base, **({"risk_game": game} if game else {})}
    out = {"key": key, "who": u["who"]}

    # ---- /profile with and without the game
    plain = client.post("/profile", json=base)
    prof = client.post("/profile", json=answers)
    if prof.status_code != 200 or plain.status_code != 200:
        f.add(key, "/profile", "HTTP 200", f"{prof.status_code} / {plain.status_code}: {prof.text[:200]}", "A")
        return out
    profile, alone = prof.json(), plain.json()
    out["profile"], out["profile_alone"] = profile, alone
    want = expected_class(profile)
    if u["choices"]["B2"] == "lt1" and profile["capacity_level"] > 2:       # a rule of its own, not read from the config
        f.add(key, "/profile", "a horizon under a year limits the capacity to level 2 however strong the rest is",
              f"capacity level {profile['capacity_level']}", "A")
    if profile["c_level"] != want:
        f.add(key, "/profile", f"category {want} by the rule of RISK_GAME_BLEND.md", f"{profile['c_level']}", "A")
    if alone["c_level"] != expected_class({**alone, "game_played": False}):
        f.add(key, "/profile (no game)", "the questionnaire alone: min(cap, tolerance)", alone["c_level"], "A")
    if game is None:
        carried = [k for k in ("game_played", "game_score", "game_level", "blended_level", "questionnaire_class") if k in profile]
        if carried:
            f.add(key, "/profile", "no game field when the game was skipped", f"carried {carried}", "B")
    elif profile.get("questionnaire_class") != alone["c_level"]:
        f.add(key, "/profile", f"questionnaire_class equals the no-game category {alone['c_level']}",
              str(profile.get("questionnaire_class")), "A")
    texts.append(f"\n## {key}: {u['who']}\n\n### /profile (category {profile['c_level']}; questionnaire alone {alone['c_level']}; "
                 f"binding {profile.get('binding_dimension')}; tolerance level {profile['tolerance_level']}, capacity level "
                 f"{profile['capacity_level']}, game level {profile.get('game_level')})\n")
    for line in profile["explanation"]:
        texts.append(f"- {line}")
    for p, s in all_strings({k: v for k, v in profile.items() if k != "explanation"}):
        if s.startswith("review") or len(s) > 40:
            texts.append(f"- ({p}) {s}")

    # ---- /preferences/form
    form_r = client.post("/preferences/form", json={"answers": answers, "trading_platforms": u["platforms"]})
    if form_r.status_code != 200:
        f.add(key, "/preferences/form", "HTTP 200", f"{form_r.status_code} {form_r.text[:200]}", "A")
        return out
    form = form_r.json()
    out["form"] = form
    offered = {q["id"]: {o["id"]: o for o in q["options"]} for q in form["questions"]}
    table = C_TABLE[int(profile["c_level"][1:])]
    texts.append(f"\n### /preferences/form ({len(form['questions'])} questions)\n")
    for q in form["questions"]:
        texts.append(f"- **{q['id']}** {q['text']}")
        for o in q["options"]:
            if not o.get("available", True):
                texts.append(f"    - NOT AVAILABLE: {o['text']} — {o.get('reason')}")
    # the answers must be offered
    usable = {q: [o for o in picked if offered.get(q, {}).get(o, {}).get("available", True)] for q, picked in u["prefs"].items()
              if q in offered}
    for q in form["questions"]:                          # what the app would not ask: questions that need an answer not given
        if q.get("requires") and not all(v in usable.get(k, []) for k, v in q["requires"].items()):
            usable.pop(q["id"], None)
    usable = {q: picked for q, picked in usable.items() if picked or q == "P1"}

    body = {"answers": answers, "preferences": {"preferences_version": pref_version, "choices": usable},
            "trading_platforms": u["platforms"], "horizon_years": u["horizon"], "liquidity_need": u["liquidity"],
            "min_cash_buffer": u["min_cash"]}
    variants = [("no goal", {}), ("goal", {"goal_amount": u["goal"], "monthly_flow": u["monthly"]})]
    if u["ibkr"]:
        variants.append(("goal + IBKR", {"goal_amount": u["goal"], "monthly_flow": u["monthly"], "ibkr_report": u["ibkr"],
                                          "ibkr_filename": f"{key}.xml"}))
    out["builds"] = {}
    for label, extra in variants:
        step = f"/portfolio ({label})"
        r = client.post("/portfolio", json={**body, **extra})
        if r.status_code != 200:
            f.add(key, step, "HTTP 200", f"{r.status_code} {r.text[:240]}", "A")
            out["builds"][label] = {"status": r.status_code, "error": r.text[:400]}
            continue
        built = r.json()
        out["builds"][label] = {"status": 200, "built": built}
        if built["c_level"] != profile["c_level"]:
            f.add(key, step, f"the same category as /profile ({profile['c_level']})", built["c_level"], "A")
        if not built["approved"]:
            f.add(key, step, "an approved portfolio", f"not approved: {built['violations'][:3]}", "B")
        check_holdings(f, u, step, built, profile)
        if "stocks" not in usable.get("P1", []) and not u["ibkr"]:          # UI-12: no stocks chosen, none held, and it is said
            held = built["report"].get("equity_weight") or 0.0
            if held > 0.005:
                f.add(key, step, "no stocks chosen: the portfolio holds none", f"stocks at {held:.1%}", "A")
            could_have = offered.get("P1", {}).get("stocks", {}).get("available", False)     # not offered at C1/C2: nothing to add
            if could_have and not any("did not choose stocks" in str(b) for b in built["report"].get("banners") or []):
                f.add(key, step, "a banner says why there are no stocks", "no such banner", "B")
        check_goal_wording(f, u, step, built)
        check_html(f, u, step, built)
        note = built.get("game_note") or ""
        if game is None and "not played" not in note:           # the contract: a skipped game is said so
            f.add(key, step, "game_note says 'Risk Game: not played' when the game was skipped", f"game_note={note!r}", "B")
        if game is not None and "Risk Game played" not in note:
            f.add(key, step, "game_note says how the played game was used", f"game_note={note!r}", "B")
        texts.append(f"\n### {step}: C{profile['c_level'][1:]}, approved={built['approved']}, "
                     f"stocks {built['report'].get('equity_weight', 0):.0%}, {len(built['report'].get('allocation') or [])} holdings\n")
        texts.append(f"- game_note: {built.get('game_note')}")
        texts.append(f"- category_basis: {(built['report'].get('category_basis') or {}).get('text')}")
        for path, s in text_of_report(built["report"]):
            texts.append(f"- ({path}) {s}")
        for m in JARGON.finditer(" ".join(s for _, s in text_of_report(built["report"]))):
            texts.append(f"- JARGON? {m.group(0)}")

    # ---- /trade-check
    main_build = (out["builds"].get("goal + IBKR") or out["builds"].get("goal") or {}).get("built")
    out["trade_checks"] = []
    history = u["ibkr"] or holding_xml(u["invested"])        # the check needs a history; users without one get their purchase
    for planned in u["trades"]:
        r = client.post("/trade-check", json={**body, "goal_amount": None, "monthly_flow": 0.0, "planned": planned,
                                              "ibkr_report": history, "ibkr_filename": f"{key}.xml"})
        if r.status_code != 200:
            out["trade_checks"].append({"planned": planned, "status": r.status_code, "error": r.text[:300]})
            f.add(key, f"/trade-check {planned}", "HTTP 200", f"{r.status_code} {r.text[:200]}", "B")
            continue
        check = r.json()["check"]
        out["trade_checks"].append({"planned": planned, "verdict": check["verdict"], "reasons": check["reasons"],
                                    "direction": check.get("direction"), "notes": check.get("notes"),
                                    "traded_r": check.get("traded_r_level"), "investor_r": check.get("investor_max_r_level")})
        inst = CATALOG.get(planned["ticker"])
        if inst and planned["side"] == "buy" and inst.r_level > table["r"] and check["verdict"] in ("ok", "approved", "fits"):
            f.add(key, f"/trade-check {planned}", f"a buy of R{inst.r_level} product is not fine for {profile['c_level']}",
                  f"verdict {check['verdict']}", "A")
        if check.get("investor_max_r_level") not in (None, table["r"]):
            f.add(key, f"/trade-check {planned}", f"investor max R-level {table['r']} (= {profile['c_level']})",
                  f"{check.get('investor_max_r_level')}", "A")
        texts.append(f"\n### /trade-check {planned['side']} {planned['amount']} {planned['ticker']}: **{check['verdict']}**\n")
        for reason in check["reasons"]:
            texts.append(f"- {reason}")
        for note in check.get("notes") or []:
            texts.append(f"- note: {note}")

    # ---- What-If through the engine (needs a goal build)
    out["what_ifs"] = []
    if main_build:
        for spec in u["what_ifs"]:
            try:
                res = dp.what_if(main_build.get("what_if_context"), spec)
            except Exception as exc:                          # noqa: BLE001 - recorded as a finding
                f.add(key, f"what-if {spec['label']}", "an answer", f"{type(exc).__name__}: {exc}"[:240], "B")
                continue
            out["what_ifs"].append(res)
            texts.append(f"\n### What-If: {spec['label']} -> status {res.get('status')}\n")
            for p, s in all_strings({k: v for k, v in res.items() if k not in ("scenario_sent_to_the_engine",)}):
                if len(s) > 25:
                    texts.append(f"- ({p}) {s}")

    # ---- the investor who replays the game
    if u["later_game"]:
        second = {**base, "risk_game": {**u["later_game"], "completed_at": GAME_DAY}}
        prof2 = client.post("/profile", json=second).json()
        r2 = client.post("/portfolio", json={**body, "answers": second, "goal_amount": u["goal"], "monthly_flow": u["monthly"]})
        out["replayed"] = {"profile": prof2, "status": r2.status_code}
        texts.append(f"\n### Replayed game ({u['later_game']}): category {prof2.get('c_level')} (before: {profile['c_level']}), "
                     f"binding {prof2.get('binding_dimension')}\n")
        for line in prof2.get("explanation", []):
            texts.append(f"- {line}")
        if r2.status_code == 200:
            built2 = r2.json()
            if built2["c_level"] != expected_class(prof2):
                f.add(key, "/portfolio after replay", f"category {expected_class(prof2)}", built2["c_level"], "A")
            check_holdings(f, u, "/portfolio after replay", built2, prof2)
            check_html(f, u, "/portfolio after replay", built2)
            out["replayed"]["built"] = built2
            texts.append(f"- game_note: {built2.get('game_note')}")
            texts.append(f"- category_basis: {(built2['report'].get('category_basis') or {}).get('text')}")
        else:
            f.add(key, "/portfolio after replay", "HTTP 200", f"{r2.status_code} {r2.text[:200]}", "A")
    return out


LEAKS = re.compile(r"(\['|\bP\d\b|Input should|Extra inputs|Field required|pydantic|Traceback|\bloc\b|NoneType|KeyError|"
                   r"\bchoices\.|\banswers\.|\bmulti_choices|\bnull\b|\bNone\b|\bValueError\b)")


def error_probes(client, f: Findings, texts: list[str]) -> None:
    """Wrong requests an app could send; the sentence the person would see is read for jargon and Python leftovers."""
    base_user = next(u for u in USERS if u["key"] == "excluder")        # a C4: industries are offered, crypto is not
    bank_version = load_question_bank().questionnaire_version
    pref_version = load_preference_bank().preferences_version
    answers = {"questionnaire_version": bank_version, "choices": base_user["choices"], "multi_choices": {"K4": base_user["k4"]},
               "amount_invested": 40_000, "amount_needed": 5_000}
    prefs = {"preferences_version": pref_version, "choices": {"P1": ["stocks", "bonds"], "P4": ["funds"]}}
    build = {"answers": answers, "preferences": prefs, "trading_platforms": ["Fidelity"], "horizon_years": 8}
    game = {"tolerance": 50, "loss_acceptance": 50, "completed_at": GAME_DAY}

    def with_answers(**changes):
        return {**build, "answers": {**answers, **changes}}

    probes = [
        ("/profile", "needs more money back than invested", {**answers, "amount_needed": 50_000}),
        ("/profile", "invests nothing", {**answers, "amount_invested": 0}),
        ("/profile", "a negative amount", {**answers, "amount_invested": -5}),
        ("/profile", "a question left out", {**answers, "choices": {k: v for k, v in answers["choices"].items() if k != "GL3"}}),
        ("/profile", "an answer that is not an option", {**answers, "choices": {**answers["choices"], "GL3": "z"}}),
        ("/profile", "an unknown question", {**answers, "choices": {**answers["choices"], "XX": "a"}}),
        ("/profile", "an old questionnaire version", {**answers, "questionnaire_version": "eq-rq-0.0.1"}),
        ("/profile", "game score above 100", {**answers, "risk_game": {**game, "tolerance": 120}}),
        ("/profile", "game score below 0", {**answers, "risk_game": {**game, "loss_acceptance": -3}}),
        ("/profile", "game played in the future", {**answers, "risk_game": {**game, "completed_at": "2030-01-01T00:00:00Z"}}),
        ("/profile", "game played long ago", {**answers, "risk_game": {**game, "completed_at": "2019-01-01T00:00:00Z"}}),
        ("/profile", "game with a text instead of a number", {**answers, "risk_game": {**game, "tolerance": "high"}}),
        ("/profile", "game with an extra field", {**answers, "risk_game": {**game, "bonus": 5}}),
        ("/preferences/form", "a platform nobody knows", {"answers": answers, "trading_platforms": ["Binanse"]}),
        ("/portfolio", "an interest and the same industry to avoid", {**build, "preferences": {**prefs, "choices": {
            "P1": ["stocks"], "P2": ["technology"], "P5": ["technology"]}}}),
        ("/portfolio", "how to hold stocks without choosing stocks", {**build, "preferences": {**prefs, "choices": {
            "P1": ["bonds"], "P4": ["funds"]}}}),
        ("/portfolio", "an option the profile does not offer (crypto for a C4)", {**build, "preferences": {**prefs, "choices": {
            "P1": ["stocks", "crypto"]}}}),
        ("/portfolio", "an option that does not exist", {**build, "preferences": {**prefs, "choices": {"P1": ["stonks"]}}}),
        ("/portfolio", "a preferences version that changed", {**build, "preferences": {**prefs, "preferences_version": "old"}}),
        ("/portfolio", "a horizon of zero", {**build, "horizon_years": 0}),
        ("/portfolio", "a goal of zero", {**build, "goal_amount": 0}),
        ("/portfolio", "no preferences and no interest areas", {k: v for k, v in build.items() if k != "preferences"}),
        ("/portfolio", "an unreadable IBKR report", {**build, "ibkr_report": "this is not a report"}),
        ("/portfolio", "an IBKR report that is an empty XML", {**build, "ibkr_report": "<FlexQueryResponse></FlexQueryResponse>"}),
        ("/portfolio", "cash buffer above 100%", {**build, "min_cash_buffer": 1.5}),
        ("/portfolio", "a goal that cannot be met with the cash asked for", {**build, "goal_amount": 50_000, "liquidity_need": 1.0}),
        ("/portfolio", "game sent twice with different values", {**build, "answers": {**answers, "risk_game": game},
                                                                 "risk_game": {**game, "tolerance": 10}}),
        ("/trade-check", "no history sent", {**build, "planned": {"ticker": "VT", "side": "buy", "amount": 100}}),
        ("/trade-check", "a ticker nobody lists", {**build, "ibkr_report": holding_xml(40_000),
                                                   "planned": {"ticker": "ZZZZ", "side": "buy", "amount": 100}}),
        ("/trade-check", "selling more than held", {**build, "ibkr_report": holding_xml(40_000),
                                                    "planned": {"ticker": "VT", "side": "sell", "amount": 900_000}}),
        ("/trade-check", "a negative amount", {**build, "ibkr_report": holding_xml(40_000),
                                               "planned": {"ticker": "VT", "side": "buy", "amount": -5}}),
    ]
    texts.append("\n## Error sentences (what a person sees when the request is wrong)\n")
    for route, what, body in probes:
        r = client.post(route, json=body)
        try:
            detail = r.json().get("detail")
        except Exception:                                    # noqa: BLE001
            detail = r.text[:200]
        if isinstance(detail, list):
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        texts.append(f"- [{route}] {what} -> HTTP {r.status_code}: {detail}")
        if r.status_code >= 500:
            f.add("errors", f"{route} ({what})", "a polite 4xx sentence", f"HTTP {r.status_code}: {str(detail)[:160]}", "A")
        elif r.status_code == 200 and what not in ("an option the profile does not offer (crypto for a C4)",
                                                   "a ticker nobody lists"):          # the check answers 200 with a blocking verdict
            f.add("errors", f"{route} ({what})", "refused with a plain sentence", "HTTP 200: it was accepted", "B")
        elif r.status_code >= 400 and detail and LEAKS.search(str(detail)):
            f.add("errors", f"{route} ({what})", "a sentence a person can read", f"HTTP {r.status_code}: {str(detail)[:200]}", "C")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--out")
    parser.add_argument("--texts")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    from main import app
    if args.as_of:
        import egoquant_routes
        pinned = date.fromisoformat(args.as_of)
        egoquant_routes._last_weekday_before = lambda day: pinned
    import egoquant_routes
    egoquant_routes._PORTFOLIO_LIMIT = 100_000           # this process only: twelve investors build far more than the public limit
    client = TestClient(app)
    findings, texts, results = Findings(), [], {}
    for u in USERS:
        if args.only and u["key"] not in args.only:
            continue
        print(f"== {u['key']}: {u['who']}", flush=True)
        results[u["key"]] = run_user(client, u, findings, texts)
        print(f"   category {results[u['key']].get('profile', {}).get('c_level')}; findings so far {len(findings.rows)}", flush=True)
    if not args.only:
        error_probes(client, findings, texts)
    for row in findings.rows:
        print(f"[{row['severity']}] {row['id']} {row['user']} | {row['step']} | expected: {row['expected']} | actual: {row['actual']}")
    print(f"\n{len(findings.rows)} automatic findings")
    if args.out:
        Path(args.out).write_text(json.dumps({"findings": findings.rows, "results": results}, indent=1, default=str), encoding="utf-8")
    if args.texts:
        Path(args.texts).write_text("\n".join(texts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
