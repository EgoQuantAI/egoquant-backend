"""Headless run of the three demo scenarios in docs/DEMO_SCRIPT.md, through the app's own routes.

    python eval/demo_pack.py                 # all scenarios
    python eval/demo_pack.py --only saver    # one scenario
    python eval/demo_pack.py --out file.json # where the raw numbers go (default: a temp file, printed)

Offline and free: no key is read, every network connection is refused, so Agent C falls back to the committed
Yahoo snapshot of REAL prices (egoquant/agents/market_intel/data/prices_snapshot_yahoo.json.gz) exactly as the
deployed app does when its feeds are down; no OpenAI call is made. The calls are the ones the frontend makes:
POST /profile, POST /preferences/form, POST /portfolio (with the goal), POST /trade-check (with an IBKR Flex
report, read-only: nothing is traded) and POST /what-if. Everything printed is read off the responses.
"""
import argparse
import json
import os
import socket
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Environment first: no keys, no background workers, no network.
for name in ("TIINGO_API_KEY", "TIINGO_WARM_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA", "EGOQUANT_DATA_FALLBACK",
             "EGOQUANT_PRICE_SNAPSHOT"):
    os.environ.pop(name, None)
os.environ.update({"EGOQUANT_WARM": "off", "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_BRIEFING_LLM": "0",
                   "EGOQUANT_BRIEFING_TONE_BACKGROUND": "off", "EGOQUANT_CONTEXT_SECRET": "demo-pack-local",
                   "EGOQUANT_CACHE_DIR": tempfile.mkdtemp(prefix="egoquant_demo_cache_")})


_LOOPBACK = ("127.0.0.1", "::1", "localhost", "0.0.0.0")
_real_connect, _real_getaddrinfo = socket.socket.connect, socket.getaddrinfo


def _host(address) -> str:
    return str(address[0]) if isinstance(address, tuple) and address else str(address)


def _guarded_connect(self, address):
    if _host(address) not in _LOOPBACK:               # asyncio's own loopback socketpair must keep working
        raise OSError("network blocked for the offline demo run")
    return _real_connect(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if str(host) not in _LOOPBACK and host is not None:
        raise OSError("network blocked for the offline demo run")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect          # type: ignore[assignment]
socket.getaddrinfo = _guarded_getaddrinfo         # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from egoquant.demo.personas import PERSONAS  # noqa: E402
from egoquant.preferences import load_preference_bank  # noqa: E402
from egoquant.risk_profile import load_question_bank  # noqa: E402

DEMO_DATA = ROOT / "docs" / "demo_data"
GAME_COMPLETED_AT = "2026-10-16T09:00:00Z"        # when the invented Risk Game was played (pinned, like the decision date)

# key -> persona, the goal the app asks after the questionnaire, the invented IBKR report to upload, one planned
# trade and one What-If question (the What-If scenario is what the advisor LLM would translate the question to).
SCENARIOS = {
    "saver": dict(persona="cautious_retiree", goal_amount=245_000, monthly_flow=0, horizon=5, liquidity=0.10,
                  min_cash=0.10, ibkr="ibkr_cautious_saver.xml",
                  trades=[dict(ticker="VT", side="buy", amount=20_000), dict(ticker="SHY", side="buy", amount=5_000)],
                  what_ifs=[dict(label="What if I add $100 a month?", extra_monthly_contribution=100),
                            dict(label="What if I need 255,000 instead?", goal_amount=255_000)],
                  game=dict(tolerance=0, loss_acceptance=50, how="pick the SAFEST card in every round")),
    "balanced": dict(persona="balanced_saver", goal_amount=120_000, monthly_flow=300, horizon=10, liquidity=0.05,
                     min_cash=0.05, ibkr="ibkr_balanced_investor.xml",
                     trades=[dict(ticker="VT", side="sell", amount=8_000), dict(ticker="VT", side="buy", amount=5_000)],
                     what_ifs=[dict(label="What if I drop all my bond funds?", exclude_assets=["bond"]),
                               dict(label="What if I add $300 a month more?", extra_monthly_contribution=300)],
                     game=dict(tolerance=50, loss_acceptance=50, how="pick the MIDDLE card in every round")),
    "beginner": dict(persona="eager_beginner", goal_amount=40_000, monthly_flow=150, horizon=10, liquidity=0.0,
                     min_cash=0.05, ibkr="ibkr_eager_beginner.xml",
                     trades=[dict(ticker="IBIT", side="buy", amount=1_000), dict(ticker="NVDA", side="sell", amount=300)],
                     what_ifs=[dict(label="What if I invest for 15 years instead of 10?", horizon_years=15),
                               dict(label="What if I add $300 a month more?", extra_monthly_contribution=300)],
                     game=dict(tolerance=0, loss_acceptance=50, how="pick the SAFEST card in every round")),
}


def questionnaire_clicks(persona) -> list[dict]:
    """The exact questions and option texts to click, in the order the app shows them."""
    bank = load_question_bank()
    rows = []
    for q in bank.questions:
        if q.type == "number":
            value = persona.amount_invested if q.id == "B3_invested" else persona.amount_needed
            rows.append({"id": q.id, "section": q.section, "question": q.text, "answer": f"{value:,.0f}"})
        elif q.type == "multi":
            picked = [o.text for o in q.options if o.id in persona.k4]
            rows.append({"id": q.id, "section": q.section, "question": q.text, "answer": "; ".join(picked)})
        else:
            rows.append({"id": q.id, "section": q.section, "question": q.text,
                         "answer": q.option(persona.choices[q.id]).text})
    return rows


def preference_clicks(persona, form: dict) -> list[dict]:
    """The preference questions the profile sees, with the option texts to tick, and what was NOT offered."""
    rows = []
    for q in form["questions"]:
        offered = {o["id"]: o for o in q["options"]}
        picked = persona.preferences.get(q["id"], [])
        rows.append({"id": q["id"], "question": q["text"], "answer": [offered[i]["text"] for i in picked if i in offered],
                     "not_offered": [{"option": o["text"], "reason": o.get("reason")} for o in q["options"]
                                     if not o.get("available", True)]})
    return rows


def summarise_portfolio(response) -> dict:
    """What the first screen shows, read off the /portfolio response."""
    if response.status_code != 200:
        return {"status": response.status_code, "error": response.text[:400]}
    r = response.json()
    rep = r["report"]
    return {
        "status": 200, "approved": r["approved"], "optimizer_status": r["status"], "c_level": r["c_level"],
        "as_of": r["as_of"], "data_source": r["data_source"], "fallback_reason": r.get("fallback_reason"),
        "violations": r["violations"], "game_note": r.get("game_note"), "goal_unavailable": r.get("goal_unavailable"),
        "equity_weight": rep["equity_weight"], "bond_weight": rep.get("bond_weight"), "cash_weight": rep.get("cash_weight"),
        "commodity_weight": rep.get("commodity_weight"), "crypto_weight": rep.get("crypto_weight"),
        "volatility": rep["ex_ante_vol"], "worst_loss": rep["worst_loss"], "worst_scenario": rep.get("worst_scenario"),
        "headline": rep["headline"],
        "goal": {k: (rep.get("goal") or {}).get(k) for k in ("chance", "in_100", "headline", "plan", "goal_amount",
                                                              "start_amount", "monthly_flow", "horizon_years")},
        "holdings": [{"id": a["asset_id"], "name": a["name"], "class": a["asset_class"], "weight": a["weight"]}
                     for a in sorted(rep.get("allocation") or [], key=lambda a: -a["weight"])],
        "limits": [{k: l.get(k) for k in ("name", "value", "limit", "direction", "ok", "source")}
                   for l in rep.get("limits") or []],
        "trading_history": r.get("trading_history"),
        "risk_engine_checks": {"passed": sum(bool(c.get("passed")) for c in r["checks"]), "total": len(r["checks"])},
        "n_instruments": len((r.get("universe") or {}).get("instruments") or []),
        "interest_areas": (r.get("universe") or {}).get("interest_areas"),
        "banners": [str(b)[:240] for b in rep.get("banners") or []],
        "category_basis": rep.get("category_basis"), "game_note": r.get("game_note"),
    }


def what_if(context: str | None, spec: dict) -> dict:
    from egoquant.agents.what_if import run_what_if
    scenario = {k: v for k, v in spec.items() if k != "label"}
    result = run_what_if(context, scenario)
    return {"question": spec["label"], "scenario_sent_to_the_engine": scenario, **result.model_dump(mode="json")}


def trade_check(client, body: dict, planned: dict) -> dict:
    from egoquant.advisor.trade_explain import trade_reply
    import asyncio
    response = client.post("/trade-check", json={**body, "goal_amount": None, "monthly_flow": 0.0, "planned": planned})
    if response.status_code != 200:
        return {"status": response.status_code, "error": response.text[:500], "planned": planned}
    data = response.json()
    check = data["check"]
    # What the chat shows when the AI is down (no key): the deterministic template of the same sealed facts.
    reply = asyncio.run(trade_reply(data["trade_context"], "Why is this flagged?", create=None, base_params={}))
    return {"status": 200, "planned": planned, "verdict": check["verdict"], "reasons": check["reasons"],
            "direction": check.get("direction"), "direction_parts": check.get("direction_parts"),
            "before": check["before"], "after": check["after"], "notes": check.get("notes"),
            "investor_max_r_level": check.get("investor_max_r_level"), "traded_r_level": check.get("traded_r_level"),
            "risk_engine_approved": check["risk_review"]["approved"], "template_reply": reply.text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", choices=list(SCENARIOS))
    parser.add_argument("--out")
    parser.add_argument("--ibkr-prices", choices=["as-deployed", "snapshot"], default="as-deployed",
                        help="as-deployed: the IBKR replay asks the live feed, which is down offline, so it values days "
                             "at trade prices (what the app does today). snapshot: the replay reads the committed "
                             "snapshot instead (what the app does when its live feed answers).")
    parser.add_argument("--as-of", help="pin the decision date (YYYY-MM-DD) instead of 'the last weekday before today'; the "
                                        "numbers move with the day otherwise (the regime model, the rate anchor)")
    args = parser.parse_args()

    from main import app
    if args.as_of:
        import egoquant_routes
        pinned = date.fromisoformat(args.as_of)
        egoquant_routes._last_weekday_before = lambda day: pinned
    if args.ibkr_prices == "snapshot":
        import egoquant_routes
        from egoquant.agents.market_intel.snapshot import agent_c_snapshot_path, load_snapshot
        snap = load_snapshot(str(agent_c_snapshot_path()))
        egoquant_routes._ibkr_prices = lambda symbols, start, end, catalog: snap.fetch_adjusted_close(
            list(symbols), start, end)
    client = TestClient(app)
    pref_version = load_preference_bank().preferences_version
    results = {}
    for key, sc in SCENARIOS.items():
        if args.only and key not in args.only:
            continue
        p = PERSONAS[sc["persona"]]
        questionnaire_only = {"questionnaire_version": load_question_bank().questionnaire_version, "choices": p.choices,
                              "multi_choices": {"K4": p.k4}, "amount_invested": p.amount_invested,
                              "amount_needed": p.amount_needed}
        game = {"tolerance": sc["game"]["tolerance"], "loss_acceptance": sc["game"]["loss_acceptance"],
                "completed_at": GAME_COMPLETED_AT}
        answers = {**questionnaire_only, "risk_game": game}        # everything after the questionnaire scores the SAME category
        out = {"persona": p.key, "description": p.description, "platforms": p.trading_platforms,
               "answers": questionnaire_clicks(p), "intake": {k: sc[k] for k in
                                                                ("goal_amount", "monthly_flow", "horizon",
                                                                 "liquidity", "min_cash")}}
        out["intake"]["game"] = dict(sc["game"])
        profile = client.post("/profile", json=answers)                         # the category after the Risk Game
        out["profile_with_game"] = profile.json()
        out["profile"] = client.post("/profile", json=questionnaire_only).json()    # the questionnaire alone (step 1)
        form = client.post("/preferences/form", json={"answers": answers, "trading_platforms": p.trading_platforms})
        out["preferences"] = preference_clicks(p, form.json())
        body = {"answers": answers,
                "preferences": {"preferences_version": pref_version, "choices": p.preferences},
                "trading_platforms": p.trading_platforms, "horizon_years": sc["horizon"],
                "liquidity_need": sc["liquidity"], "min_cash_buffer": sc["min_cash"],
                "goal_amount": sc["goal_amount"], "monthly_flow": sc["monthly_flow"]}
        report_text = (DEMO_DATA / sc["ibkr"]).read_text(encoding="utf-8")
        for variant, extra in (("without_ibkr", {}), ("with_ibkr", {"ibkr_report": report_text,
                                                                    "ibkr_filename": sc["ibkr"]})):
            built = client.post("/portfolio", json={**body, **extra})
            out[variant] = summarise_portfolio(built)
            if variant == "with_ibkr" and built.status_code == 200:
                r = built.json()
                out["what_ifs"] = [what_if(r.get("what_if_context"), spec) for spec in sc["what_ifs"]]
                out["trade_checks"] = [trade_check(client, {**body, **extra}, planned) for planned in sc["trades"]]
        results[key] = out
        w = out["with_ibkr"]
        print(f"{key}: /profile {profile.status_code} {out['profile'].get('c_level')} -> {out['profile_with_game'].get('c_level')}"
              f" with the game, /portfolio {w['status']}"
              + (f" approved={w['approved']} chance={w['goal'].get('chance')}" if w["status"] == 200 else ""))
    from egoquant.agents.market_intel.snapshot import agent_c_snapshot_path, load_snapshot
    snapshot = load_snapshot(str(agent_c_snapshot_path()))
    from egoquant.settings import portfolio_method
    method = portfolio_method()
    results["_meta"] = {"made_by": ("EGOQUANT_PORTFOLIO_METHOD=v2 " if method == "v2" else "") + "python eval/demo_pack.py"
                        + (" --ibkr-prices snapshot" if args.ibkr_prices == "snapshot" else ""), "method": method,
                        "prices": snapshot.label, "prices_file": agent_c_snapshot_path().name,
                        "network": "blocked; no key; no OpenAI call", "ibkr_replay_prices": args.ibkr_prices,
                        "as_of": args.as_of or "the last weekday before the run", "risk_game": "played in all three scenarios"}
    path = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "demo_pack_numbers.json"
    path.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print("raw numbers:", path)
    sys.stdout.flush()
    os._exit(0)          # a stray background thread (the directory loader) must not keep a headless run alive


if __name__ == "__main__":
    raise SystemExit(main())
