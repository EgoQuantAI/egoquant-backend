"""Deploy check: run ONCE after a Railway redeploy, against the deployed API. Read-only, no secrets, standard library only.

    python eval/deploy_check.py https://<the-railway-backend>            # everything, about 3 minutes
    python eval/deploy_check.py https://<the-railway-backend> --quick    # no portfolio builds (status, headers, docs, areas)
    python eval/deploy_check.py http://127.0.0.1:8000 --python 3.13      # against a local server (rehearsal)

Prints PASS / FAIL / WARN / SKIP per check and exits 1 when anything FAILs. It sends the same requests the web app sends,
with the three invented demo investors of docs/DEMO_SCRIPT.md (answers in docs/demo_data/deploy_check_requests.json, the
invented IBKR files next to it). Nothing is stored (no account, no token), no AI is called, and nothing is traded.

What is checked
  GET  /data/status          price_mode, Python version, portfolio method, news worker state, the time budgets, the Risk Game
                             weight and whether it may raise the category
  GET  /docs, /openapi.json  reachable
  headers / CORS             security headers; the Vercel origin is allowed, a foreign one is not, no credentials
  POST /preferences/areas    {areas, limit: 8}
  POST /profile  x3 x2       the demo answers WITH the Risk Game answer 200, game_played true and the pack's category after the game;
                             the same answers WITHOUT it carry no game field and give the questionnaire's category (an old
                             backend does not know answers.risk_game and answers 422)
  POST /portfolio  x3        each demo investor builds in <= 30 s, approved, the saved-prices note is shown, and the top-line
                             numbers match the demo pack (docs/demo_data/demo_numbers_v1.json or _v2.json, whichever matches the
                             server's portfolio method)
  POST /portfolio  + IBKR    the balanced investor with the invented IBKR file: Agent B's behavioural limit is there
  POST /portfolio  x1        20 interest areas: <= 30 s and the "we used 8 of 20 areas" banner
  POST /trade-check x3       one planned trade per investor: the pack's verdict, and a sealed chat context
  POST /what-if              the route answers 401 without a sign-in (the what-if engine needs an account) and /portfolio
                             handed out its sealed what-if context

What "equal to the demo pack" means: the pack was run with the decision date pinned to 2026-10-16 (the last close in the saved
prices); the server uses the last weekday before today. The categories, the approval, the Risk Engine's passed/total, the trade
verdicts and the Agent B limit must be EQUAL; the goal chance, the stock share and the volatility must be within the tolerances
below (TOLERANCES), which cover that date difference.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "demo_data"
REQUESTS_FILE = DATA / "deploy_check_requests.json"
DEFAULT_ORIGIN = "https://fintechaton-frontend.vercel.app"
BUILD_SECONDS = 30.0                      # a /portfolio build or a trade check must answer within this long
AREAS_LIMIT = 8                           # PORTFOLIO_MAX_INTEREST_AREAS
# How far the deployed numbers may be from the pack's (PROVISIONAL). Measured 20 Oct: moving the decision date from the pack's
# 16 Oct to 19 Oct changed the goal chance by at most 1 point and nothing else (stock share, volatility, Agent B's limit: 0.0);
# the margin below covers another few days of drift and the Python/numpy version of the server.
TOLERANCES = {"goal_chance_points": 3.0, "stock_share_points": 2.0, "volatility_points": 0.5, "behavioural_limit_points": 0.3}
SECURITY_HEADERS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                    "referrer-policy": "strict-origin-when-cross-origin"}
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    seconds: float | None = None


@dataclass
class Reply:
    status: int                           # 0 = no answer (network error or timeout)
    headers: dict                         # lower-case names
    body: bytes
    seconds: float
    error: str = ""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


# ==========================================================================
# HTTP (standard library only)
# ==========================================================================
def call(base: str, method: str, path: str, body: dict | None = None, headers: dict | None = None,
         timeout: float = 90.0) -> Reply:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    request.add_header("User-Agent", "egoquant-deploy-check/1")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(),
                         time.monotonic() - started)
    except urllib.error.HTTPError as error:
        return Reply(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read(), time.monotonic() - started)
    except Exception as error:                              # DNS, refused, timeout, TLS
        return Reply(0, {}, b"", time.monotonic() - started, f"{type(error).__name__}: {error}"[:200])


# ==========================================================================
# Judging (pure functions of parsed answers; these are what the tests cover)
# ==========================================================================
def judge_status(status: dict | None, expect_price_mode: str, expect_python: str, expect_method: str,
                 expect_news_worker: str) -> list[Result]:
    if not isinstance(status, dict):
        return [Result("GET /data/status answers JSON", FAIL, "no JSON object")]
    out = [Result("GET /data/status answers JSON", PASS)]
    mode = status.get("price_mode")
    out.append(Result(f"price_mode is {expect_price_mode}", PASS if mode == expect_price_mode else FAIL, f"price_mode={mode!r}"))
    version = str(status.get("python_version", ""))
    out.append(Result(f"Python version starts with {expect_python}", PASS if version.startswith(expect_python) else FAIL,
                      f"python_version={version!r}"))
    method = status.get("portfolio_method")
    out.append(Result(f"portfolio_method is {expect_method}", PASS if method == expect_method else FAIL,
                      f"portfolio_method={method!r}"))
    worker = status.get("news_worker")
    state = worker.get("state") if isinstance(worker, dict) else None
    if state is None:
        out.append(Result("news worker state is reported", FAIL, f"news_worker={worker!r}"))
    else:
        extra = ""
        if isinstance(worker, dict):
            extra = ", ".join(f"{k}={worker[k]}" for k in ("reason", "runs", "pairs_rated", "pairs_failed", "last_run_seconds",
                                                          "last_error") if worker.get(k) not in (None, 0, ""))
        wanted = expect_news_worker == "any" or state == expect_news_worker
        out.append(Result("news worker state" + ("" if expect_news_worker == "any" else f" is {expect_news_worker}"),
                          PASS if wanted else FAIL, f"state={state!r}" + (f" ({extra})" if extra else "")))
        if isinstance(worker, dict) and worker.get("pairs_failed", 0) and not worker.get("pairs_rated", 0):
            out.append(Result("news worker rates headlines", WARN,
                              f"pairs_rated 0 and pairs_failed {worker.get('pairs_failed')}: every rating failed (credit?)"))
    out += judge_game_settings(status)
    budgets = {k: status.get(k) for k in ("live_budget_seconds", "slow_request_seconds") if k in status}
    out.append(Result("slow-feed guard settings are reported", PASS if len(budgets) == 2 else WARN, f"{budgets}"))
    return out


GAME_WEIGHT = 0.3333                         # the default share of the Risk Game in the tolerance part (the demo pack assumes it)
GAME_FIELDS = ("game_played", "game_score", "game_level", "blended_level", "questionnaire_class")


def judge_game_settings(status: dict) -> list[Result]:
    """risk_game_weight and risk_game_may_raise of /data/status: WARN (not FAIL) when they differ from what the demo pack assumes."""
    weight, raises = status.get("risk_game_weight"), status.get("risk_game_may_raise")
    out = []
    if not isinstance(weight, (int, float)) or isinstance(weight, bool):
        out.append(Result("Risk Game weight is reported", WARN, f"risk_game_weight={weight!r} (an old backend?)"))
    else:
        same = abs(weight - GAME_WEIGHT) <= 0.0005
        out.append(Result(f"Risk Game weight is {GAME_WEIGHT}", PASS if same else WARN,
                          f"risk_game_weight={weight}" + ("" if same else ": differs from the demo pack, whose categories assume 0.3333")))
    if not isinstance(raises, bool):
        out.append(Result("Risk Game may-raise is reported", WARN, f"risk_game_may_raise={raises!r} (an old backend?)"))
    else:
        out.append(Result("Risk Game may raise the category", PASS if raises else WARN,
                          f"risk_game_may_raise={raises}" + ("" if raises else ": the game can only LOWER the category, unlike the demo pack's rule")))
    return out


def judge_profile(with_game: "Reply", without_game: "Reply", expected_with: str, expected_without: str, label: str) -> list[Result]:
    """POST /profile with the demo answers plus the Risk Game, and without it (what an old backend cannot do)."""
    out = []
    name = f"{label}: POST /profile with the Risk Game"
    if with_game.status == 422:
        body = with_game.json()
        detail = str(body.get("detail", ""))[:90] if isinstance(body, dict) else ""
        out.append(Result(name, FAIL, "HTTP 422: this backend does not know answers.risk_game (is the backend new? deploy it before the "
                                      "frontend)" + (f" [{detail}]" if detail else "")))
    elif with_game.status != 200:
        out.append(Result(name, FAIL, f"HTTP {with_game.status}" + (f" ({with_game.error})" if with_game.error else "")))
    else:
        data = with_game.json() if isinstance(with_game.json(), dict) else {}
        played = data.get("game_played") is True and all(data.get(k) is not None for k in GAME_FIELDS)
        out.append(Result(f"{name} is played and gives {expected_with}", PASS if played and data.get("c_level") == expected_with else FAIL,
                          f"game_played={data.get('game_played')!r}, c_level={data.get('c_level')!r}, game_level={data.get('game_level')!r}, "
                          f"blended_level={data.get('blended_level')!r}"))
        out.append(Result(f"{label}: the questionnaire alone would give {expected_without} (questionnaire_class)",
                          PASS if data.get("questionnaire_class") == expected_without else FAIL,
                          f"questionnaire_class={data.get('questionnaire_class')!r}"))
    name = f"{label}: POST /profile without the game"
    if without_game.status != 200:
        out.append(Result(name, FAIL, f"HTTP {without_game.status}"))
    else:
        data = without_game.json() if isinstance(without_game.json(), dict) else {}
        carried = [k for k in GAME_FIELDS if data.get(k) not in (None, False)]
        out.append(Result(f"{name} carries no game field and gives {expected_without}",
                          PASS if not carried and data.get("c_level") == expected_without else FAIL,
                          f"c_level={data.get('c_level')!r}" + (f", game fields present: {carried}" if carried else "")))
    return out


def judge_security_headers(headers: dict) -> list[Result]:
    missing = [f"{k}: {v}" for k, v in SECURITY_HEADERS.items() if headers.get(k) != v]
    if not headers.get("permissions-policy"):
        missing.append("permissions-policy")
    return [Result("security headers", PASS if not missing else FAIL, "all present" if not missing else "missing or wrong: "
                   + "; ".join(missing))]


def judge_cors(allowed: Reply, foreign: Reply, origin: str) -> list[Result]:
    a = allowed.headers
    ok = (allowed.status in (200, 204) and a.get("access-control-allow-origin") == origin
          and "POST" in a.get("access-control-allow-methods", "").upper())
    out = [Result(f"CORS: {origin} is allowed to POST", PASS if ok else FAIL,
                  f"HTTP {allowed.status}, allow-origin={a.get('access-control-allow-origin')!r}, "
                  f"methods={a.get('access-control-allow-methods')!r}")]
    out.append(Result("CORS: no credentials are allowed", PASS if a.get("access-control-allow-credentials", "").lower() != "true"
                      else FAIL, f"allow-credentials={a.get('access-control-allow-credentials')!r}"))
    leaked = foreign.headers.get("access-control-allow-origin")
    out.append(Result("CORS: a foreign origin gets no allow-origin header", PASS if not leaked else FAIL,
                      f"allow-origin={leaked!r}"))
    return out


def snapshot_note(report: dict, price_mode: str) -> Result:
    banners = [str(b) for b in (report or {}).get("banners") or []]
    calm = next((b for b in banners if b.startswith("Prices:") and "saved snapshot" in b), None)
    amber = next((b for b in banners if b.startswith("LIVE PRICES UNAVAILABLE")), None)
    if price_mode == "snapshot":
        if calm:
            return Result("saved-prices note is shown", PASS, calm[:120])
        if amber:
            return Result("saved-prices note is shown", WARN, "the amber fallback banner, not the calm snapshot-mode note: " + amber[:100])
        return Result("saved-prices note is shown", FAIL, "no 'Prices: saved snapshot ...' banner")
    if calm or amber:
        return Result("saved-prices note is shown", PASS, (calm or amber)[:120])
    return Result("saved-prices note is shown", WARN, "none (live prices?)")


def _points(a: float, b: float) -> float:
    return abs(a - b) * 100.0


def compare_portfolio(reply: dict, pack: dict, label: str) -> list[Result]:
    """The deployed /portfolio answer against the demo pack's entry for the same request (pack = numbers[key][variant])."""
    out = []
    out.append(Result(f"{label}: approved by the Risk Engine", PASS if reply.get("approved") is True else FAIL,
                      f"approved={reply.get('approved')!r}, status={reply.get('status')!r}"))
    checks = reply.get("checks") or []
    passed = sum(bool(c.get("passed")) for c in checks)
    out.append(Result(f"{label}: every Risk Engine check passes", PASS if checks and passed == len(checks) else FAIL,
                      f"{passed} of {len(checks)} (the pack: {pack['risk_engine_checks']['passed']} of "
                      f"{pack['risk_engine_checks']['total']}; the number of checks follows the number of holdings)"))
    report = reply.get("report") or {}
    chance, pack_chance = (report.get("goal") or {}).get("in_100"), (pack.get("goal") or {}).get("in_100")
    if isinstance(chance, (int, float)) and isinstance(pack_chance, (int, float)):
        gap = abs(chance - pack_chance)
        out.append(Result(f"{label}: goal chance within {TOLERANCES['goal_chance_points']:g} points of the pack",
                          PASS if gap <= TOLERANCES["goal_chance_points"] else FAIL, f"{chance} in 100 (pack {pack_chance})"))
    else:
        out.append(Result(f"{label}: goal chance within {TOLERANCES['goal_chance_points']:g} points of the pack", FAIL,
                          f"deployed {chance!r}, pack {pack_chance!r}"))
    for key, pack_key, tolerance, name in (("equity_weight", "equity_weight", "stock_share_points", "stock share"),
                                           ("ex_ante_vol", "volatility", "volatility_points", "volatility")):
        value, expected = report.get(key), pack.get(pack_key)
        if isinstance(value, (int, float)) and isinstance(expected, (int, float)):
            gap = _points(value, expected)
            out.append(Result(f"{label}: {name} within {TOLERANCES[tolerance]:g} points of the pack",
                              PASS if gap <= TOLERANCES[tolerance] else FAIL,
                              f"{value * 100:.1f}% (pack {expected * 100:.1f}%)"))
        else:
            out.append(Result(f"{label}: {name} within {TOLERANCES[tolerance]:g} points of the pack", FAIL,
                              f"deployed {value!r}, pack {expected!r}"))
    return out


def behavioural_limit(limits: list | None) -> float | None:
    for item in limits or []:
        if "behavioural" in str(item.get("name", "")).lower():
            return item.get("limit")
    return None


def judge_areas_banner(report: dict, asked: int = 20, limit: int = AREAS_LIMIT) -> Result:
    banners = [str(b) for b in (report or {}).get("banners") or []]
    hit = next((b for b in banners if f"cover {asked} interest areas" in b and f"we used {limit} of them" in b), None)
    return Result(f"banner says the first {limit} of {asked} areas were used", PASS if hit else FAIL,
                  (hit or "no such banner; banners: " + " | ".join(b[:60] for b in banners))[:200])


def judge_areas_answer(answer, limit: int = AREAS_LIMIT) -> Result:
    ok = (isinstance(answer, dict) and answer.get("limit") == limit and isinstance(answer.get("areas"), list)
          and bool(answer["areas"]) and all(isinstance(a, str) for a in answer["areas"]))
    return Result(f"POST /preferences/areas answers {{areas, limit: {limit}}}", PASS if ok else FAIL,
                  f"limit={answer.get('limit')!r}, {len(answer.get('areas') or [])} areas" if isinstance(answer, dict)
                  else f"answer={answer!r}")


def judge_trade(reply: dict, pack_trade: dict, label: str) -> list[Result]:
    check = (reply or {}).get("check") or {}
    verdict = check.get("verdict")
    out = [Result(f"{label}: verdict is the pack's ({pack_trade['verdict']})", PASS if verdict == pack_trade["verdict"] else FAIL,
                  f"verdict={verdict!r}")]
    out.append(Result(f"{label}: the check has reasons and a sealed chat context",
                      PASS if check.get("reasons") and (reply or {}).get("trade_context") else FAIL,
                      f"{len(check.get('reasons') or [])} reasons, trade_context {'present' if (reply or {}).get('trade_context') else 'missing'}"))
    return out


def judge_whatif(unsigned: Reply, context) -> list[Result]:
    detail = ""
    body = unsigned.json()
    if isinstance(body, dict):
        detail = str(body.get("detail", ""))[:100]
    ok = unsigned.status in (401, 403) and bool(detail)
    return [Result("/portfolio hands out a sealed what-if context", PASS if isinstance(context, str) and context else FAIL,
                   "present" if context else "missing"),
            Result("POST /what-if without a sign-in is refused politely", PASS if ok else FAIL,
                   f"HTTP {unsigned.status}: {detail or unsigned.error or unsigned.body[:80]!r}")]


# ==========================================================================
# The run
# ==========================================================================
def load_requests() -> dict:
    return json.loads(REQUESTS_FILE.read_text(encoding="utf-8"))


def pick_numbers(method: str, override: str | None) -> tuple[dict, str]:
    path = Path(override) if override else DATA / f"demo_numbers_{method if method in ('v1', 'v2') else 'v1'}.json"
    return json.loads(path.read_text(encoding="utf-8")), path.name


def timed(name: str, reply: Reply, limit: float | None, ok_status: int = 200) -> Result:
    """The HTTP-level result of one request: answered with the expected status, within the limit."""
    if reply.status == 0:
        return Result(name, FAIL, f"no answer after {reply.seconds:.0f} s ({reply.error})", reply.seconds)
    if reply.status != ok_status:
        body = reply.json()
        detail = body.get("detail") if isinstance(body, dict) else reply.body[:100]
        return Result(name, FAIL, f"HTTP {reply.status}: {str(detail)[:140]}", reply.seconds)
    if limit is not None and reply.seconds > limit:
        return Result(name, FAIL, f"answered in {reply.seconds:.1f} s, more than {limit:g} s", reply.seconds)
    return Result(name, PASS, f"answered in {reply.seconds:.1f} s" if limit is not None else "", reply.seconds)


def run(base: str, args) -> list[Result]:
    results: list[Result] = []

    def add(*items: Result) -> None:
        for item in items:
            results.append(item)
            print(f"{item.status:<5} {item.name}" + (f"  [{item.detail}]" if item.detail else ""), flush=True)

    def section(title: str) -> None:
        print(f"\n== {title}", flush=True)

    requests_data = load_requests()
    section("Server facts")
    status_reply = call(base, "GET", "/data/status", timeout=args.timeout)
    add(timed("GET /data/status answers", status_reply, 10.0))
    status = status_reply.json() if status_reply.status == 200 else None
    add(*judge_status(status, args.price_mode, args.python, args.method, args.news_worker))
    method = (status or {}).get("portfolio_method") or args.method
    server_date = status_reply.headers.get("date", "?")
    print(f"      facts: server date {server_date}; price_mode={(status or {}).get('price_mode')}; "
          f"portfolio_method={method}; python {(status or {}).get('python_version')}; "
          f"risk_game_weight={(status or {}).get('risk_game_weight')}; risk_game_may_raise={(status or {}).get('risk_game_may_raise')}",
          flush=True)
    docs = call(base, "GET", "/docs", timeout=args.timeout)
    add(Result("GET /docs is reachable", PASS if docs.status == 200 and b"swagger" in docs.body.lower() else FAIL,
               f"HTTP {docs.status}", docs.seconds))
    spec = call(base, "GET", "/openapi.json", timeout=args.timeout)
    info = (spec.json() or {}).get("info", {}) if spec.status == 200 and isinstance(spec.json(), dict) else {}
    add(Result("GET /openapi.json is reachable", PASS if info else FAIL, f"{info.get('title')} {info.get('version')}", spec.seconds))

    section("Headers and CORS")
    add(*judge_security_headers(status_reply.headers))
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    allowed = call(base, "OPTIONS", "/portfolio", headers={**preflight, "Origin": args.origin}, timeout=args.timeout)
    foreign = call(base, "OPTIONS", "/portfolio", headers={**preflight, "Origin": "https://evil.example"}, timeout=args.timeout)
    add(*judge_cors(allowed, foreign, args.origin))

    section("Interest areas")
    areas = call(base, "POST", "/preferences/areas", requests_data["areas_probe"], timeout=args.timeout)
    add(timed("POST /preferences/areas answers", areas, 15.0))
    if areas.status == 200:
        add(judge_areas_answer(areas.json()))

    numbers, numbers_name = pick_numbers(method, args.numbers)
    section("Risk Game in the category")
    for key in ("saver", "balanced", "beginner"):
        answers = requests_data["personas"][key]["body"]["answers"]
        plain = {k: v for k, v in answers.items() if k != "risk_game"}
        with_game = call(base, "POST", "/profile", answers, timeout=args.timeout)
        without_game = call(base, "POST", "/profile", plain, timeout=args.timeout)
        after = (numbers[key].get("profile_with_game") or numbers[key]["profile"])["c_level"]
        add(*judge_profile(with_game, without_game, after, numbers[key]["profile"]["c_level"], key))

    if args.quick:
        print("\n(--quick: portfolio builds, trade checks and what-if skipped)")
        return results

    # Builds on a server that is not in snapshot mode send LIVE price requests with the production key (quota, ~15 s each):
    # the check does not do that unless asked. The wrong mode itself is already a FAIL above.
    server_mode = (status or {}).get("price_mode")
    if server_mode != "snapshot" and not args.allow_live:
        add(Result("portfolio builds skipped", WARN, f"the server's price_mode is {server_mode!r}, so builds would send live price "
                   "requests with the production key; set EGOQUANT_PRICE_MODE=snapshot, or pass --allow-live to build anyway"))
        return results

    print(f"\n(demo pack compared with: {numbers_name}, method {numbers['_meta'].get('method', 'v1')}, decision date "
          f"{numbers['_meta'].get('as_of', 'not pinned')}; deployed numbers use the server's own date)")
    contexts: dict[str, dict] = {}
    for key in ("saver", "balanced", "beginner"):
        spec_ = requests_data["personas"][key]
        section(f"Demo investor '{key}' ({spec_['persona']}): build")
        reply = call(base, "POST", "/portfolio", spec_["body"], timeout=args.timeout)
        add(timed(f"{key}: POST /portfolio answers within {BUILD_SECONDS:g} s", reply, BUILD_SECONDS))
        data = reply.json() if reply.status == 200 else None
        if not isinstance(data, dict):
            continue
        contexts[key] = data
        print(f"      facts: as_of {data.get('as_of')}, data_source {data.get('data_source')}, "
              f"fallback_reason {data.get('fallback_reason')!r}, {len((data.get('universe') or {}).get('instruments') or [])} instruments",
              flush=True)
        expected_class = (numbers[key].get("profile_with_game") or numbers[key]["profile"])["c_level"]     # after the Risk Game
        add(Result(f"{key}: category is {expected_class}", PASS if data.get("c_level") == expected_class else FAIL,
                   f"c_level={data.get('c_level')!r}"))
        add(snapshot_note(data.get("report") or {}, (status or {}).get("price_mode", args.price_mode)))
        add(*compare_portfolio(data, numbers[key]["without_ibkr"], key))

    section("Balanced investor with the invented IBKR file (Agent B)")
    spec_ = requests_data["personas"]["balanced"]
    ibkr_body = {**spec_["body"], "ibkr_report": (DATA / spec_["ibkr_file"]).read_text(encoding="utf-8"),
                 "ibkr_filename": spec_["ibkr_file"]}
    reply = call(base, "POST", "/portfolio", ibkr_body, timeout=args.timeout)
    add(timed(f"balanced + IBKR: POST /portfolio answers within {BUILD_SECONDS:g} s", reply, BUILD_SECONDS))
    data = reply.json() if reply.status == 200 else None
    if isinstance(data, dict):
        add(*compare_portfolio(data, numbers["balanced"]["with_ibkr"], "balanced + IBKR"))
        got, want = behavioural_limit((data.get("report") or {}).get("limits")), behavioural_limit(numbers["balanced"]["with_ibkr"]["limits"])
        if got is None:
            add(Result("balanced + IBKR: Agent B's behavioural limit is there", FAIL, "no behavioural limit in the Limits"))
        else:
            gap = _points(got, want) if want is not None else None
            add(Result(f"balanced + IBKR: Agent B's behavioural limit equals the pack's ({want * 100:.1f}%)"
                       if want is not None else "balanced + IBKR: Agent B's behavioural limit is there",
                       PASS if gap is None or gap <= TOLERANCES["behavioural_limit_points"] else FAIL,
                       f"{got * 100:.1f}%"))
        contexts["balanced_ibkr"] = data

    section("20 interest areas")
    reply = call(base, "POST", "/portfolio", requests_data["twenty_areas"]["body"], timeout=args.timeout)
    add(timed(f"POST /portfolio with 20 areas answers within {BUILD_SECONDS:g} s", reply, BUILD_SECONDS))
    data = reply.json() if reply.status == 200 else None
    if isinstance(data, dict):
        add(judge_areas_banner(data.get("report") or {}))
        add(Result("20 areas: approved", PASS if data.get("approved") else FAIL, f"approved={data.get('approved')!r}"))

    section("Trade checks")
    for key in ("saver", "balanced", "beginner"):
        spec_ = requests_data["personas"][key]
        body = {**spec_["body"], "goal_amount": None, "monthly_flow": 0.0, "planned": spec_["trade"],
                "ibkr_report": (DATA / spec_["ibkr_file"]).read_text(encoding="utf-8"), "ibkr_filename": spec_["ibkr_file"]}
        reply = call(base, "POST", "/trade-check", body, timeout=args.timeout)
        add(timed(f"{key}: POST /trade-check answers within {BUILD_SECONDS:g} s", reply, BUILD_SECONDS))
        if reply.status == 200:
            add(*judge_trade(reply.json(), numbers[key]["trade_checks"][0], f"{key} {spec_['trade']['side']} {spec_['trade']['ticker']}"))

    section("What-if")
    context = (contexts.get("balanced_ibkr") or contexts.get("balanced") or {}).get("what_if_context")
    unsigned = call(base, "POST", "/what-if", {"what_if_context": context or "x" * 20, "scenario": {"extra_monthly_contribution": 100}},
                    timeout=args.timeout)
    add(*judge_whatif(unsigned, context))
    return results


def summary(results: list[Result]) -> str:
    counts = {s: sum(r.status == s for r in results) for s in (PASS, FAIL, WARN, SKIP)}
    slow = sorted((r for r in results if r.seconds is not None and r.seconds >= 5.0), key=lambda r: -r.seconds)[:6]
    lines = [f"\n{counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[WARN]} WARN, {counts[SKIP]} SKIP"]
    if slow:
        lines.append("slowest answers: " + "; ".join(f"{r.name.split(':')[0]} {r.seconds:.1f} s" for r in slow))
    lines.append("DEPLOY CHECK " + ("FAILED" if counts[FAIL] else "PASSED"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("url", nargs="?", help="the deployed API, e.g. https://....up.railway.app (no trailing path)")
    ap.add_argument("--price-mode", default="snapshot", help="expected price_mode (default snapshot)")
    ap.add_argument("--python", default="3.14", help="expected start of python_version (default 3.14)")
    ap.add_argument("--method", default="v1", help="expected portfolio_method (default v1)")
    ap.add_argument("--news-worker", default="any", help="expected news worker state: any, off, idle or running (default any)")
    ap.add_argument("--origin", default=DEFAULT_ORIGIN, help="the web origin that must be allowed by CORS")
    ap.add_argument("--numbers", help="a demo_numbers file to compare with (default: the one for the server's method)")
    ap.add_argument("--timeout", type=float, default=120.0, help="real seconds to wait for one request (default 120)")
    ap.add_argument("--quick", action="store_true", help="skip the portfolio builds, trade checks and what-if")
    ap.add_argument("--allow-live", action="store_true",
                    help="build portfolios even when the server is not in snapshot mode (this spends live price requests)")
    ap.add_argument("--write-requests", action="store_true",
                    help="(developers) rebuild docs/demo_data/deploy_check_requests.json from the demo personas; needs the repo's venv")
    args = ap.parse_args(argv)
    if args.write_requests:
        REQUESTS_FILE.write_text(json.dumps(build_requests(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("written", REQUESTS_FILE.relative_to(ROOT))
        return 0
    if not args.url or not args.url.startswith(("http://", "https://")):
        ap.error("give the deployed API's address, e.g. https://....up.railway.app")
    print(f"Deploy check of {args.url}  (read-only; nothing is stored, no AI is called)\n")
    results = run(args.url, args)
    print(summary(results))
    return 1 if any(r.status == FAIL for r in results) else 0


# ==========================================================================
# The requests file (developers only: needs the repo's Python packages)
# ==========================================================================
TWENTY_AREAS = ["stocks", "bonds", "cash", "technology", "healthcare", "financials", "energy", "consumer", "industrials",
                "utilities", "communication", "materials", "real_estate", "gold", "precious_metals", "commodities",
                "crypto", "altcoins", "us_stocks", "international_stocks"]


def build_requests() -> dict:
    """The /portfolio bodies of the three demo investors (exactly what eval/demo_pack.py sends), the 20-area body and the
    /preferences/areas probe. Read from the committed demo pack and the demo personas."""
    sys.path.insert(0, str(ROOT))
    from egoquant.demo.personas import PERSONAS
    from egoquant.preferences import load_preference_bank
    from egoquant.risk_profile import load_question_bank

    numbers = json.loads((DATA / "demo_numbers_v1.json").read_text(encoding="utf-8"))
    version, pref_version = load_question_bank().questionnaire_version, load_preference_bank().preferences_version

    def answers(p, game: dict | None = None) -> dict:
        out = {"questionnaire_version": version, "choices": p.choices, "multi_choices": {"K4": p.k4},
               "amount_invested": p.amount_invested, "amount_needed": p.amount_needed}
        if game:                                      # the demo investors play the Risk Game (the pack's scores)
            out["risk_game"] = {"tolerance": game["tolerance"], "loss_acceptance": game["loss_acceptance"],
                                "completed_at": "2026-10-16T09:00:00Z"}
        return out

    personas = {}
    ibkr = {"saver": "ibkr_cautious_saver.xml", "balanced": "ibkr_balanced_investor.xml", "beginner": "ibkr_eager_beginner.xml"}
    for key in ("saver", "balanced", "beginner"):
        p, intake = PERSONAS[numbers[key]["persona"]], numbers[key]["intake"]
        personas[key] = {
            "persona": p.key, "ibkr_file": ibkr[key], "trade": numbers[key]["trade_checks"][0]["planned"],
            "body": {"answers": answers(p, intake.get("game")), "preferences": {"preferences_version": pref_version, "choices": p.preferences},
                     "trading_platforms": p.trading_platforms, "horizon_years": intake["horizon"],
                     "liquidity_need": intake["liquidity"], "min_cash_buffer": intake["min_cash"],
                     "goal_amount": intake["goal_amount"], "monthly_flow": intake["monthly_flow"]}}
    crypto = PERSONAS["crypto_trader"]
    growth = PERSONAS["growth_investor"]
    return {
        "_meta": {"made_by": "python eval/deploy_check.py --write-requests", "from": "docs/demo_data/demo_numbers_v1.json + egoquant.demo.personas"},
        "personas": personas,
        "twenty_areas": {"body": {"answers": answers(crypto), "interest_areas": TWENTY_AREAS,
                                  "trading_platforms": crypto.trading_platforms, "horizon_years": 10,
                                  "liquidity_need": 0.0, "min_cash_buffer": 0.05}},
        "areas_probe": {"answers": answers(growth), "trading_platforms": growth.trading_platforms,
                        "preferences": {"preferences_version": pref_version, "choices": growth.preferences}},
    }


if __name__ == "__main__":
    raise SystemExit(main())
