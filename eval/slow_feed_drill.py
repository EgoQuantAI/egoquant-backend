"""Slow-feed drill: what a /portfolio build does when the live price feed answers, but slowly.

    python eval/slow_feed_drill.py                 # all scenarios, each in its own process
    python eval/slow_feed_drill.py --only twenty_areas
    python eval/slow_feed_drill.py --json drill.json

Why: on the deployed backend a /portfolio with 20 interest areas took about 195 s (19 Oct night). Core funds and holdings are
priced live one by one at about 15 s a request, and a slow but SUCCESSFUL request never switched the build to the
committed snapshot (only failures did). The web proxy gives up at 120 s.

How, with no real waiting and no network: the "live" Tiingo feed is a fake `requests.Session.get` that answers every
request correctly (with the committed snapshot's real prices, so the data is right) but costs 15 SECONDS ON A FAKE CLOCK.
`time.monotonic`, `time.time` and `time.sleep` are replaced by that clock for the whole process, and Agent C is given the
same clock if it takes one (`clock=`). Binance answers HTTP 451 at once, as it does from a US server. Everything else is
the real code: the route `egoquant_routes.portfolio`, Agent C, Agent A, Agent B, the Risk Engine, the report.

What must hold (the budget is SIMULATED seconds on the fake clock):
  * the build finishes within 30 s: at most two live requests are waited for, then the WHOLE build uses the snapshot;
  * the answer says so: the report carries the amber "LIVE PRICES UNAVAILABLE ... saved real prices as of <date>" banner,
    the price note names the snapshot date, and the data is never synthetic;
  * the portfolio is still a real one: approved by the Risk Engine, weights sum to 1.
Scenarios: the app's flow (preference answers, with a goal), 20 interest areas, the same with portfolio method v2, a build
with current holdings (priced live even after the time cap), and snapshot price mode (EGOQUANT_PRICE_MODE=snapshot: no
live request at all).

Exit code 1 when a scenario FAILs. A WARN (the API's plain label does not name the slow feed) does not fail the drill.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 16)                      # the snapshot's last close
REQUEST_SECONDS = 15.0                           # what one live request costs on the fake clock
BUDGET_SECONDS = 30.0                            # a build must finish within this many simulated seconds
FAKE_KEY = "drill-fake-key-not-a-secret"
TWENTY_AREAS = ["stocks", "bonds", "cash", "technology", "healthcare", "financials", "energy", "consumer", "industrials",
                "utilities", "communication", "materials", "real_estate", "gold", "precious_metals", "commodities",
                "crypto", "altcoins", "us_stocks", "international_stocks"]
HOLDINGS = {"VT": 0.30, "VTI": 0.20, "AGG": 0.20, "IAU": 0.10, "QQQ": 0.10, "BIL": 0.10}
SECRET_ENV = ("TIINGO_API_KEY", "TIINGO_WARM_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA", "EGOQUANT_DATA_FALLBACK",
              "EGOQUANT_PRICE_SNAPSHOT", "EGOQUANT_PRICE_MODE", "EGOQUANT_PORTFOLIO_METHOD")

# name -> (persona, request extras, environment, what it shows)
SCENARIOS = {
    "app_flow_with_goal": dict(persona="growth_investor", mode="preferences", goal=True, env={},
                               what="the app's own flow: preference answers, a 10-year goal"),
    "twenty_areas": dict(persona="crypto_trader", mode="areas", goal=False, env={},
                         what="20 interest areas (Aslan's case: 195 s deployed)"),
    "twenty_areas_v2": dict(persona="crypto_trader", mode="areas", goal=False, env={"EGOQUANT_PORTFOLIO_METHOD": "v2"},
                            what="the same with portfolio method v2 (the whole eligible universe)"),
    "holdings": dict(persona="growth_investor", mode="preferences", goal=False, holdings=True, env={},
                     what="current holdings: priced live even after the time cap"),
    "snapshot_mode": dict(persona="growth_investor", mode="preferences", goal=False,
                          env={"EGOQUANT_PRICE_MODE": "snapshot"}, expect_live_requests=0,
                          what="EGOQUANT_PRICE_MODE=snapshot: the committed prices, no live request at all"),
}


# ==========================================================================
# One scenario (this is what a subprocess does)
# ==========================================================================
class FakeClock:
    """A clock that only moves when something waits for it."""

    def __init__(self, monotonic: float, wall: float):
        self.base_monotonic, self.base_wall, self.elapsed = monotonic, wall, 0.0

    def monotonic(self) -> float:
        return self.base_monotonic + self.elapsed

    def time(self) -> float:
        return self.base_wall + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code, self._payload, self.text = status, payload, "" if payload is None else "ok"

    def json(self):
        return self._payload


def run_scenario(name: str) -> dict:
    import functools
    import inspect
    import logging
    import time

    spec = SCENARIOS[name]
    for var in SECRET_ENV:
        os.environ.pop(var, None)
    os.environ.update({"TIINGO_API_KEY": FAKE_KEY, "EGOQUANT_WARM": "off", "EGOQUANT_NEWS_WORKER": "off",
                       "EGOQUANT_BRIEFING": "off", "EGOQUANT_BRIEFING_LLM": "0", "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off",
                       "EGOQUANT_CONTEXT_SECRET": "slow-feed-drill", "EGOQUANT_CACHE_DIR": tempfile.mkdtemp(prefix="drill_")})
    os.environ.update(spec["env"])
    sys.path.insert(0, str(ROOT))
    logging.disable(logging.WARNING)

    clock = FakeClock(time.monotonic(), time.time())
    time.monotonic, time.time, time.sleep = clock.monotonic, clock.time, clock.sleep

    import requests

    from egoquant.agents.market_intel import agent as agent_module
    from egoquant.agents.market_intel.snapshot import agent_c_snapshot_path, load_snapshot

    snapshot = load_snapshot(str(agent_c_snapshot_path()))
    calls: list[dict] = []

    def fake_get(self, url, params=None, **kwargs):
        if "api.tiingo.com/tiingo/daily/" not in url:
            return FakeResponse(451)                       # Binance from a US server, the directory's sources, ...
        ticker = url.split("/daily/")[1].split("/")[0].upper()
        clock.advance(REQUEST_SECONDS)
        calls.append({"ticker": ticker, "at": round(clock.elapsed, 1)})
        start, end = date.fromisoformat(params["startDate"]), date.fromisoformat(params["endDate"])
        try:
            frame = snapshot.fetch_adjusted_close([ticker], start, end)
        except Exception:
            return FakeResponse(404)
        if ticker not in frame.columns:
            return FakeResponse(404)
        series = frame[ticker].dropna()
        return FakeResponse(200, [{"date": f"{d:%Y-%m-%d}T00:00:00.000Z", "adjClose": float(v)} for d, v in series.items()])

    requests.sessions.Session.get = fake_get                # type: ignore[assignment]

    # Agent C is given the fake clock when it takes one (the slow-feed guard measures with it)
    original = agent_module.MarketIntelligenceAgent.__init__
    if "clock" in inspect.signature(original).parameters:
        @functools.wraps(original)
        def with_clock(self, *args, **kwargs):
            kwargs.setdefault("clock", clock.monotonic)
            return original(self, *args, **kwargs)
        agent_module.MarketIntelligenceAgent.__init__ = with_clock          # type: ignore[assignment]

    import egoquant_routes as routes
    from egoquant.demo.personas import PERSONAS
    from egoquant.preferences import load_preference_bank
    from egoquant.risk_profile import load_question_bank

    routes._directory_for_request = lambda start=True: None
    routes._last_weekday_before = lambda day: AS_OF

    p = PERSONAS[spec["persona"]]
    body = {"answers": {"questionnaire_version": load_question_bank().questionnaire_version, "choices": p.choices,
                        "multi_choices": {"K4": p.k4}, "amount_invested": p.amount_invested,
                        "amount_needed": p.amount_needed},
            "trading_platforms": p.trading_platforms, "horizon_years": 10.0, "liquidity_need": p.liquidity_need,
            "min_cash_buffer": p.min_cash_buffer}
    if spec["mode"] == "areas":
        body["interest_areas"] = TWENTY_AREAS
    else:
        body["preferences"] = {"preferences_version": load_preference_bank().preferences_version, "choices": p.preferences}
    if spec.get("goal"):
        body.update(goal_amount=round(p.amount_invested * 1.6, -2), monthly_flow=0.0)
    if spec.get("holdings"):
        body["current_holdings"] = HOLDINGS

    out = routes.portfolio(routes.PortfolioIn(**body))
    report = out.report
    banners = [str(b) for b in report.get("banners") or []]
    return {
        "scenario": name, "what": spec["what"], "simulated_seconds": round(clock.elapsed, 1), "live_requests": len(calls),
        "live_tickers": [c["ticker"] for c in calls][:12], "approved": bool(out.approved), "status": out.status,
        "data_source": out.data_source, "fallback_reason": out.fallback_reason,
        "banners": banners, "weights_sum": round(sum(a["weight"] for a in report.get("allocation") or []), 4),
        "n_holdings": len(report.get("allocation") or []), "universe": len((out.universe or {}).get("instruments") or []),
        "expect_live_requests": spec.get("expect_live_requests"),
    }


# ==========================================================================
# Judging (pure, tested without running a build)
# ==========================================================================
def judge(result: dict) -> list[tuple[str, str, str]]:
    """[(check, PASS | FAIL | WARN, detail)] for one scenario's result."""
    checks = []
    seconds, requests_made = result["simulated_seconds"], result["live_requests"]
    checks.append(("finishes within the simulated budget", "PASS" if seconds <= BUDGET_SECONDS else "FAIL",
                   f"{seconds:g} s on the fake clock ({requests_made} live request(s) of {REQUEST_SECONDS:g} s); "
                   f"budget {BUDGET_SECONDS:g} s"))
    expected = result.get("expect_live_requests")
    if expected is not None:
        checks.append((f"makes {expected} live request(s)", "PASS" if requests_made == expected else "FAIL",
                       f"made {requests_made}"))
    else:
        checks.append(("waits for at most two live requests", "PASS" if requests_made <= 2 else "FAIL",
                       f"waited for {requests_made}: " + ", ".join(result["live_tickers"][:6])
                       + (" ..." if requests_made > 6 else "")))
    banner = next((b for b in result["banners"] if b.startswith("LIVE PRICES UNAVAILABLE")), None)
    if result.get("expect_live_requests") == 0:
        quiet = [b for b in result["banners"] if "saved real prices" in b or "real prices as of" in b]
        checks.append(("says the prices are the saved real ones", "PASS" if banner or quiet else "FAIL",
                       (banner or (quiet[0] if quiet else "no banner or note about saved prices"))[:150]))
    else:
        good = banner is not None and "saved real prices as of" in banner and str(AS_OF) in banner
        checks.append(("falls back to the snapshot and says so (amber banner)", "PASS" if good else "FAIL",
                       (banner or "no LIVE PRICES UNAVAILABLE banner")[:150]))
    checks.append(("prices the investor's choices, not only the 4 core funds", "PASS" if result["universe"] > 4 else "FAIL",
                   f"{result['universe']} instruments priced"))
    checks.append(("the data is real, never synthetic", "PASS" if result["data_source"] != "synthetic" else "FAIL",
                   f"data_source={result['data_source']}"))
    checks.append(("the portfolio is real and approved", "PASS" if result["approved"] and abs(result["weights_sum"] - 1) < 0.01
                   else "FAIL", f"approved={result['approved']}, {result['n_holdings']} holdings, weights sum "
                                f"{result['weights_sum']}"))
    if result.get("expect_live_requests") != 0:
        label = result.get("fallback_reason")
        plain = bool(label) and any(w in label.lower() for w in ("slow", "too long", "time"))
        checks.append(("the API's plain label names the slow feed", "PASS" if plain else "WARN",
                       f"fallback_reason={label!r}"))
    return checks


def verdict(checks) -> str:
    return "FAIL" if any(status == "FAIL" for _, status, _ in checks) else "PASS"


# ==========================================================================
# Command line
# ==========================================================================
def run_in_subprocess(name: str, timeout: float = 900.0) -> dict:
    with tempfile.TemporaryDirectory(prefix="drill_out_") as tmp:
        out = Path(tmp) / "result.json"
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", name, "--out", str(out)]
        try:
            done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            return {"scenario": name, "error": f"did not finish in {timeout:.0f} real seconds"}
        if done.returncode != 0 or not out.exists():
            lines = [x for x in (done.stderr or done.stdout).strip().splitlines() if x.strip()]
            return {"scenario": name, "error": (lines[-1] if lines else f"exit code {done.returncode}")[:300]}
        return json.loads(out.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", nargs="*", choices=list(SCENARIOS))
    ap.add_argument("--json", help="write the results here")
    ap.add_argument("--worker", choices=list(SCENARIOS), help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.worker:
        Path(args.out).write_text(json.dumps(run_scenario(args.worker)), encoding="utf-8")
        sys.stdout.flush()
        os._exit(0)                                       # a stray non-daemon thread must not keep the run alive

    names = args.only or list(SCENARIOS)
    print(f"Slow-feed drill: every live price request costs {REQUEST_SECONDS:g} simulated seconds; a build must finish "
          f"within {BUDGET_SECONDS:g}.\n")
    failed, results = False, []
    for name in names:
        result = run_in_subprocess(name)
        if "error" in result:
            print(f"FAIL  {name}: could not run: {result['error']}\n")
            failed = True
            results.append(result)
            continue
        checks = judge(result)
        result["checks"] = checks
        results.append(result)
        status = verdict(checks)
        failed |= status == "FAIL"
        print(f"{status}  {name}: {result['what']}")
        for check, state, detail in checks:
            print(f"        {state:<5} {check}: {detail}")
        print()
    print("DRILL " + ("FAILED" if failed else "PASSED"))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
