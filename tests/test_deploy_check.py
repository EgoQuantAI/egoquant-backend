"""eval/deploy_check.py: the judging of the deployed server's answers (status, headers, CORS, the demo investors' numbers against the
demo pack, the 20-area banner, trade checks, what-if), the requests file, and one whole run against a simulated deployment.
No network: `call` is replaced by a fake server built from the demo pack itself."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "demo_data"


def load():
    spec = importlib.util.spec_from_file_location("deploy_check", ROOT / "eval" / "deploy_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dc = load()
PACK = json.loads((DATA / "demo_numbers_v1.json").read_text(encoding="utf-8"))
ORIGIN = dc.DEFAULT_ORIGIN
GOOD_STATUS = {"state": "off", "news_worker": {"state": "off", "reason": "EGOQUANT_NEWS_WORKER=off"}, "price_mode": "snapshot",
               "live_budget_seconds": 15.0, "slow_request_seconds": 8.0, "portfolio_method": "v1", "python_version": "3.14.0",
               "risk_game_weight": 0.3333, "risk_game_may_raise": True}
CALM = "Prices: saved snapshot of real prices as of 16 Oct 2026, Yahoo Finance; crypto from Binance. Markets after 16 Oct 2026 are not in it."


def states(results):
    return {r.name: r.status for r in results}


def only(results, start):
    found = [r for r in results if r.name.startswith(start)]
    assert len(found) == 1, [r.name for r in results]
    return found[0]


# ---- the server facts -------------------------------------------------------------------------------------------------
def test_a_correct_deployment_passes_every_status_check():
    results = dc.judge_status(GOOD_STATUS, "snapshot", "3.14", "v1", "any")
    assert {r.status for r in results} == {dc.PASS}, [(r.name, r.detail) for r in results]


@pytest.mark.parametrize("change, name", [
    ({"price_mode": "live"}, "price_mode is snapshot"),
    ({"python_version": "3.13.5"}, "Python version starts with 3.14"),
    ({"portfolio_method": "v2"}, "portfolio_method is v1"),
])
def test_each_wrong_server_fact_fails_its_own_check(change, name):
    results = dc.judge_status({**GOOD_STATUS, **change}, "snapshot", "3.14", "v1", "any")
    assert states(results)[name] == dc.FAIL and sum(r.status == dc.FAIL for r in results) == 1


def test_the_news_worker_state_is_reported_expected_or_warned_about():
    off = dc.judge_status(GOOD_STATUS, "snapshot", "3.14", "v1", "off")
    assert states(off)["news worker state is off"] == dc.PASS
    running = {**GOOD_STATUS, "news_worker": {"state": "idle", "runs": 3, "pairs_rated": 0, "pairs_failed": 1500,
                                              "last_run_seconds": 3037.0}}
    results = dc.judge_status(running, "snapshot", "3.14", "v1", "any")
    assert states(results)["news worker state"] == dc.PASS and "pairs_failed=1500" in only(results, "news worker state").detail
    assert only(results, "news worker rates headlines").status == dc.WARN                  # every rating failed: the credit
    assert states(dc.judge_status(running, "snapshot", "3.14", "v1", "off"))["news worker state is off"] == dc.FAIL
    assert only(dc.judge_status({**GOOD_STATUS, "news_worker": None}, "snapshot", "3.14", "v1", "any"), "news worker").status == dc.FAIL


def test_a_status_that_is_not_json_fails_at_once():
    assert [r.status for r in dc.judge_status(None, "snapshot", "3.14", "v1", "any")] == [dc.FAIL]
    assert [r.status for r in dc.judge_status([1, 2], "snapshot", "3.14", "v1", "any")] == [dc.FAIL]


# ---- headers and CORS -------------------------------------------------------------------------------------------------
GOOD_HEADERS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY",
                "referrer-policy": "strict-origin-when-cross-origin", "permissions-policy": "camera=()"}


def test_security_headers_must_all_be_there():
    assert dc.judge_security_headers(GOOD_HEADERS)[0].status == dc.PASS
    for missing in ("x-frame-options", "permissions-policy", "x-content-type-options"):
        broken = {k: v for k, v in GOOD_HEADERS.items() if k != missing}
        result = dc.judge_security_headers(broken)[0]
        assert result.status == dc.FAIL and missing in result.detail
    assert dc.judge_security_headers({**GOOD_HEADERS, "x-frame-options": "SAMEORIGIN"})[0].status == dc.FAIL


def reply(status=200, headers=None, body=b"", seconds=0.1):
    return dc.Reply(status, headers or {}, body, seconds)


def test_cors_allows_the_web_origin_only_and_never_credentials():
    ok = reply(200, {"access-control-allow-origin": ORIGIN, "access-control-allow-methods": "GET, POST, DELETE, OPTIONS"})
    nothing = reply(400, {})
    results = dc.judge_cors(ok, nothing, ORIGIN)
    assert {r.status for r in results} == {dc.PASS}
    assert dc.judge_cors(nothing, nothing, ORIGIN)[0].status == dc.FAIL
    credentials = reply(200, {**ok.headers, "access-control-allow-credentials": "true"})
    assert only(dc.judge_cors(credentials, nothing, ORIGIN), "CORS: no credentials").status == dc.FAIL
    leaky = reply(200, {"access-control-allow-origin": "*"})
    assert only(dc.judge_cors(ok, leaky, ORIGIN), "CORS: a foreign origin").status == dc.FAIL


# ---- the saved-prices note ----------------------------------------------------------------------------------------------
def test_snapshot_mode_expects_the_calm_note_and_accepts_the_amber_one_with_a_warning():
    assert dc.snapshot_note({"banners": ["DRAFT LIMITS", CALM]}, "snapshot").status == dc.PASS
    amber = "LIVE PRICES UNAVAILABLE: this portfolio uses saved real prices as of 2026-10-16, cached."
    assert dc.snapshot_note({"banners": [amber]}, "snapshot").status == dc.WARN
    assert dc.snapshot_note({"banners": ["DRAFT LIMITS"]}, "snapshot").status == dc.FAIL
    assert dc.snapshot_note({}, "snapshot").status == dc.FAIL
    assert dc.snapshot_note({"banners": []}, "live").status == dc.WARN


# ---- the numbers against the demo pack ----------------------------------------------------------------------------------
def answer_from(pack: dict, **changes) -> dict:
    """What /portfolio would answer for a pack entry (the fields the check reads)."""
    out = {"approved": pack["approved"], "status": "optimal", "c_level": "C3",
           "checks": [{"passed": True}] * pack["risk_engine_checks"]["total"],
           "report": {"banners": [CALM], "equity_weight": pack["equity_weight"], "ex_ante_vol": pack["volatility"],
                      "goal": {"in_100": pack["goal"]["in_100"]}, "limits": pack["limits"]}}
    out.update(changes)
    return out


def test_the_pack_itself_passes_and_the_tolerances_are_the_stated_ones():
    pack = PACK["balanced"]["without_ibkr"]
    assert {r.status for r in dc.compare_portfolio(answer_from(pack), pack, "balanced")} == {dc.PASS}
    assert dc.TOLERANCES == {"goal_chance_points": 3.0, "stock_share_points": 2.0, "volatility_points": 0.5,
                             "behavioural_limit_points": 0.3}


def test_each_number_has_its_own_edge():
    pack = PACK["balanced"]["without_ibkr"]

    def verdict(mutate, start):
        answer = answer_from(pack)
        mutate(answer)
        return only(dc.compare_portfolio(answer, pack, "b"), start).status

    base_chance = pack["goal"]["in_100"]
    assert verdict(lambda a: a["report"]["goal"].update(in_100=base_chance + 3), "b: goal chance") == dc.PASS
    assert verdict(lambda a: a["report"]["goal"].update(in_100=base_chance - 4), "b: goal chance") == dc.FAIL
    assert verdict(lambda a: a["report"].update(equity_weight=pack["equity_weight"] + 0.019), "b: stock share") == dc.PASS
    assert verdict(lambda a: a["report"].update(equity_weight=pack["equity_weight"] + 0.021), "b: stock share") == dc.FAIL
    assert verdict(lambda a: a["report"].update(ex_ante_vol=pack["volatility"] + 0.0049), "b: volatility") == dc.PASS
    assert verdict(lambda a: a["report"].update(ex_ante_vol=pack["volatility"] + 0.0051), "b: volatility") == dc.FAIL
    assert verdict(lambda a: a.update(approved=False), "b: approved") == dc.FAIL
    assert verdict(lambda a: a["checks"].__setitem__(0, {"passed": False}), "b: every Risk Engine check") == dc.FAIL
    assert verdict(lambda a: a.update(checks=[]), "b: every Risk Engine check") == dc.FAIL
    assert verdict(lambda a: a["report"].update(goal={}), "b: goal chance") == dc.FAIL            # missing number


def test_the_behavioural_limit_is_found_by_name():
    limits = PACK["balanced"]["with_ibkr"]["limits"]
    assert dc.behavioural_limit(limits) == pytest.approx(0.0884, abs=1e-3)
    assert dc.behavioural_limit(PACK["saver"]["with_ibkr"]["limits"]) is None
    assert dc.behavioural_limit(None) is None


# ---- 20 areas, /preferences/areas, trade checks, what-if ------------------------------------------------------------------
BANNER_20 = ("Your choices cover 20 interest areas; to keep this fast we used 8 of them (stocks, bonds) for prices and news and "
             "left out industrials. Choose fewer areas to include them all.")


def test_the_twenty_area_banner_must_say_eight_of_twenty():
    assert dc.judge_areas_banner({"banners": ["x", BANNER_20]}).status == dc.PASS
    assert dc.judge_areas_banner({"banners": ["x"]}).status == dc.FAIL
    assert dc.judge_areas_banner({"banners": [BANNER_20.replace("8 of them", "5 of them")]}).status == dc.FAIL
    assert dc.judge_areas_banner({}).status == dc.FAIL


def test_the_areas_answer_must_be_a_list_and_a_limit_of_eight():
    assert dc.judge_areas_answer({"areas": ["stocks", "gold"], "limit": 8}).status == dc.PASS
    for bad in ({"areas": ["stocks"], "limit": 20}, {"areas": [], "limit": 8}, {"areas": [1], "limit": 8}, {"limit": 8},
                [], None):
        assert dc.judge_areas_answer(bad).status == dc.FAIL


def test_a_trade_check_needs_the_packs_verdict_reasons_and_a_chat_context():
    pack_trade = PACK["saver"]["trade_checks"][0]
    good = {"check": {"verdict": pack_trade["verdict"], "reasons": [{"message": "x"}]}, "trade_context": "sealed"}
    assert {r.status for r in dc.judge_trade(good, pack_trade, "t")} == {dc.PASS}
    assert dc.judge_trade({**good, "check": {**good["check"], "verdict": "advisable"}}, pack_trade, "t")[0].status == dc.FAIL
    assert dc.judge_trade({**good, "trade_context": None}, pack_trade, "t")[1].status == dc.FAIL
    assert dc.judge_trade({"check": {"verdict": pack_trade["verdict"], "reasons": []}, "trade_context": "s"}, pack_trade, "t")[1].status == dc.FAIL


def test_the_what_if_route_must_refuse_politely_without_a_sign_in():
    signed_out = reply(401, body=json.dumps({"detail": "Please sign in."}).encode())
    assert {r.status for r in dc.judge_whatif(signed_out, "sealed")} == {dc.PASS}
    assert dc.judge_whatif(signed_out, None)[0].status == dc.FAIL
    assert dc.judge_whatif(reply(500, body=b"boom"), "sealed")[1].status == dc.FAIL
    assert dc.judge_whatif(reply(200, body=b"{}"), "sealed")[1].status == dc.FAIL            # it must NOT answer without a token
    assert dc.judge_whatif(dc.Reply(0, {}, b"", 1.0, "ConnectionError"), "sealed")[1].status == dc.FAIL


def test_the_time_limit_and_the_status_of_a_request():
    assert dc.timed("t", reply(200, seconds=29.9), 30.0).status == dc.PASS
    assert dc.timed("t", reply(200, seconds=30.1), 30.0).status == dc.FAIL
    assert dc.timed("t", reply(502, body=json.dumps({"detail": "gateway"}).encode()), 30.0).status == dc.FAIL
    assert dc.timed("t", dc.Reply(0, {}, b"", 120.0, "TimeoutError"), 30.0).status == dc.FAIL
    assert dc.timed("t", reply(401), None, ok_status=401).status == dc.PASS


# ---- the numbers file and the requests file --------------------------------------------------------------------------------
def test_the_pack_is_chosen_by_the_servers_method():
    assert dc.pick_numbers("v1", None)[1] == "demo_numbers_v1.json" and dc.pick_numbers("v2", None)[1] == "demo_numbers_v2.json"
    assert dc.pick_numbers("whatever", None)[1] == "demo_numbers_v1.json"
    assert dc.pick_numbers("v1", str(DATA / "demo_numbers_v2.json"))[1] == "demo_numbers_v2.json"


def test_the_requests_file_is_what_the_demo_personas_send():
    committed = json.loads(dc.REQUESTS_FILE.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(dc.build_requests(), sort_keys=True)), \
        "deploy_check_requests.json is stale: python eval/deploy_check.py --write-requests"
    assert len(committed["twenty_areas"]["body"]["interest_areas"]) == 20
    assert {k: v["trade"]["ticker"] for k, v in committed["personas"].items()} == {"saver": "VT", "balanced": "VT", "beginner": "IBIT"}
    for persona in committed["personas"].values():                         # every IBKR file the script reads exists
        assert (DATA / persona["ibkr_file"]).is_file()


# ---- a whole run against a simulated deployment ------------------------------------------------------------------------------
GOAL_TO_KEY = {PACK[k]["intake"]["goal_amount"]: k for k in ("saver", "balanced", "beginner")}


class FakeServer:
    def __init__(self, status=None, seconds=1.0, wrong_origin=False, signed_out_status=401, knows_the_game=True,
                 game_changes_nothing=False):
        self.status, self.seconds, self.wrong_origin, self.signed_out_status = status or GOOD_STATUS, seconds, wrong_origin, signed_out_status
        self.knows_the_game, self.game_changes_nothing = knows_the_game, game_changes_nothing
        self.log = []

    def __call__(self, base, method, path, body=None, headers=None, timeout=0):
        self.log.append((method, path))
        json_headers = dict(GOOD_HEADERS)
        s = self.seconds

        def ok(data):
            return dc.Reply(200, json_headers, json.dumps(data).encode(), s)
        if (method, path) == ("GET", "/data/status"):
            return ok(self.status)
        if (method, path) == ("GET", "/docs"):
            return dc.Reply(200, {}, b"<html>swagger-ui</html>", 0.1)
        if (method, path) == ("GET", "/openapi.json"):
            return ok({"info": {"title": "EgoQuant API", "version": "0.1.0"}})
        if method == "OPTIONS":
            origin = (headers or {}).get("Origin")
            allowed = origin == ORIGIN and not self.wrong_origin
            return dc.Reply(200 if allowed else 400, {"access-control-allow-origin": origin,
                                                      "access-control-allow-methods": "GET, POST, DELETE, OPTIONS"} if allowed else {}, b"", 0.1)
        if path == "/preferences/areas":
            return ok({"areas": ["stocks", "bonds"], "limit": 8})
        if path == "/profile":
            key = next(k for k in ("saver", "balanced", "beginner")
                       if float(next(a for a in PACK[k]["answers"] if a["id"] == "B3_invested")["answer"].replace(",", ""))
                       == body["amount_invested"])
            if "risk_game" in body:
                if not self.knows_the_game:                                      # an old backend: extra fields are refused
                    return dc.Reply(422, json_headers, json.dumps({"detail": "risk_game: Extra inputs are not permitted"}).encode(), 0.1)
                return ok(PACK[key]["profile"] if self.game_changes_nothing else PACK[key]["profile_with_game"])
            return ok(PACK[key]["profile"])
        if path == "/portfolio":
            if "interest_areas" in body:
                return ok(answer_from(PACK["balanced"]["without_ibkr"], c_level="C5", report={
                    **answer_from(PACK["balanced"]["without_ibkr"])["report"], "banners": [CALM, BANNER_20]}))
            key = GOAL_TO_KEY[body["goal_amount"]]
            variant = "with_ibkr" if body.get("ibkr_report") else "without_ibkr"
            answer = answer_from(PACK[key][variant], c_level=(PACK[key].get("profile_with_game") or PACK[key]["profile"])["c_level"],
                                 what_if_context="sealed-what-if",
                                 as_of="2026-10-19", data_source="historical", fallback_reason=None, universe={"instruments": []})
            return ok(answer)
        if path == "/trade-check":
            trade = next(t for key in ("saver", "balanced", "beginner") for t in PACK[key]["trade_checks"]
                         if t["planned"]["ticker"] == body["planned"]["ticker"] and t["planned"]["side"] == body["planned"]["side"])
            return ok({"check": {"verdict": trade["verdict"], "reasons": [{"message": "x"}]}, "trade_context": "sealed"})
        if path == "/what-if":
            return dc.Reply(self.signed_out_status, json_headers, json.dumps({"detail": "Please sign in."}).encode(), 0.1)
        raise AssertionError(f"unexpected request {method} {path}")


def run_against(server, monkeypatch, capsys, *extra):
    monkeypatch.setattr(dc, "call", server)
    code = dc.main(["https://example.invalid", "--python", "3.14", *extra])
    return code, capsys.readouterr().out


def test_a_whole_run_against_a_correct_simulated_deployment_passes(monkeypatch, capsys):
    server = FakeServer()
    code, out = run_against(server, monkeypatch, capsys)
    assert code == 0, out
    assert "DEPLOY CHECK PASSED" in out and "0 FAIL" in out
    posts = [p for m, p in server.log if m == "POST"]
    assert posts.count("/portfolio") == 5 and posts.count("/trade-check") == 3 and "/what-if" in posts     # 3 + IBKR + 20 areas
    assert posts.count("/profile") == 6                                                                    # 3 investors, with and without the game


def test_the_quick_mode_builds_nothing(monkeypatch, capsys):
    server = FakeServer()
    code, out = run_against(server, monkeypatch, capsys, "--quick")
    assert code == 0 and "/portfolio" not in [p for m, p in server.log if m == "POST"] and "skipped" in out
    assert [p for m, p in server.log if m == "POST"].count("/profile") == 6                 # the Risk Game step also runs in quick mode


def test_a_slow_deployment_fails_the_time_checks(monkeypatch, capsys):
    code, out = run_against(FakeServer(seconds=31.0), monkeypatch, capsys)
    assert code == 1 and "answers within 30 s" in out and out.count("FAIL") >= 8 and "DEPLOY CHECK FAILED" in out


def test_a_wrong_python_or_mode_or_origin_is_caught_in_a_run(monkeypatch, capsys):
    code, out = run_against(FakeServer(status={**GOOD_STATUS, "python_version": "3.13.5", "price_mode": "live"}), monkeypatch, capsys, "--quick")
    assert code == 1 and "FAIL  Python version starts with 3.14" in out and "FAIL  price_mode is snapshot" in out
    code, out = run_against(FakeServer(wrong_origin=True), monkeypatch, capsys, "--quick")
    assert code == 1 and "FAIL  CORS" in out


def test_no_portfolio_is_built_on_a_server_that_would_send_live_price_requests(monkeypatch, capsys):
    live = {**GOOD_STATUS, "price_mode": "live"}
    server = FakeServer(status=live)
    code, out = run_against(server, monkeypatch, capsys)
    assert code == 1 and "FAIL  price_mode is snapshot" in out and "portfolio builds skipped" in out
    assert "/portfolio" not in [p for m, p in server.log if m == "POST"]
    server = FakeServer(status=live)                                   # the operator may still ask for it
    code, out = run_against(server, monkeypatch, capsys, "--allow-live")
    assert [p for m, p in server.log if m == "POST"].count("/portfolio") == 5


def test_a_what_if_route_that_answers_without_a_token_is_a_failure(monkeypatch, capsys):
    code, out = run_against(FakeServer(signed_out_status=200), monkeypatch, capsys)
    assert code == 1 and "FAIL  POST /what-if without a sign-in" in out


def test_the_address_is_required_and_must_be_a_url(capsys):
    with pytest.raises(SystemExit):
        dc.main([])
    with pytest.raises(SystemExit):
        dc.main(["railway.app"])


# ---- the Risk Game settings of /data/status and the /profile step ----------------------------------------------------------
def test_the_risk_game_settings_of_the_status_are_checked_and_only_warn():
    assert [r.status for r in dc.judge_game_settings(GOOD_STATUS)] == [dc.PASS, dc.PASS]
    results = dc.judge_game_settings({**GOOD_STATUS, "risk_game_weight": 0.2, "risk_game_may_raise": False})
    assert [r.status for r in results] == [dc.WARN, dc.WARN] and "differs from the demo pack" in results[0].detail
    assert "only LOWER" in results[1].detail
    assert [r.status for r in dc.judge_game_settings({**GOOD_STATUS, "risk_game_weight": 0.33333333})] == [dc.PASS, dc.PASS]
    assert dc.judge_game_settings({**GOOD_STATUS, "risk_game_weight": 0.0})[0].status == dc.WARN             # the kill switch is on
    old = {k: v for k, v in GOOD_STATUS.items() if not k.startswith("risk_game")}                        # an old backend
    assert [r.status for r in dc.judge_game_settings(old)] == [dc.WARN, dc.WARN]
    assert [r.status for r in dc.judge_game_settings({**GOOD_STATUS, "risk_game_weight": "0.33", "risk_game_may_raise": "yes"})] == [dc.WARN, dc.WARN]
    in_status = dc.judge_status({**GOOD_STATUS, "risk_game_weight": 0.5}, "snapshot", "3.14", "v1", "any")
    assert any(r.name.startswith("Risk Game weight") and r.status == dc.WARN for r in in_status)
    assert not any(r.status == dc.FAIL for r in in_status)                                                 # a WARN never fails the run


def profile_reply(key, game: bool, **changes):
    data = dict(PACK[key]["profile_with_game" if game else "profile"])
    data.update(changes)
    return reply(200, body=json.dumps(data).encode())


def test_the_profile_step_wants_the_game_to_count_and_the_game_free_call_to_stay_as_it_was():
    for key, after, alone in (("saver", "C2", "C2"), ("balanced", "C3", "C3"), ("beginner", "C4", "C5")):
        results = dc.judge_profile(profile_reply(key, True), profile_reply(key, False), after, alone, key)
        assert {r.status for r in results} == {dc.PASS}, [(r.name, r.detail) for r in results]
    # an old backend refuses answers.risk_game: 422, with the plain hint to deploy the backend first
    old = reply(422, body=json.dumps({"detail": "risk_game: Extra inputs are not permitted"}).encode())
    results = dc.judge_profile(old, profile_reply("saver", False), "C2", "C2", "saver")
    assert results[0].status == dc.FAIL and "422" in results[0].detail and "deploy it before the frontend" in results[0].detail
    assert results[-1].status == dc.PASS                                                      # the game-free call still works
    # the game is known but did not count (the weight is 0): game_played false and the wrong category
    ignored = reply(200, body=json.dumps({**PACK["beginner"]["profile"], "game_played": False}).encode())
    assert dc.judge_profile(ignored, profile_reply("beginner", False), "C4", "C5", "beginner")[0].status == dc.FAIL
    # another category than the pack's
    assert dc.judge_profile(profile_reply("balanced", True, c_level="C4"), profile_reply("balanced", False), "C3", "C3",
                            "balanced")[0].status == dc.FAIL
    assert dc.judge_profile(profile_reply("saver", True, questionnaire_class="C3"), profile_reply("saver", False), "C2", "C2",
                            "saver")[1].status == dc.FAIL
    # a profile without a game that carries game fields, or another category
    leaky = profile_reply("saver", False, game_level=2)
    assert dc.judge_profile(profile_reply("saver", True), leaky, "C2", "C2", "saver")[-1].status == dc.FAIL
    assert dc.judge_profile(profile_reply("saver", True), profile_reply("saver", False, c_level="C3"), "C2", "C2", "saver")[-1].status == dc.FAIL
    nothing = dc.Reply(0, {}, b"", 1.0, "ConnectionError")
    assert dc.judge_profile(nothing, nothing, "C2", "C2", "saver")[0].status == dc.FAIL
    assert dc.judge_profile(profile_reply("saver", True), reply(500), "C2", "C2", "saver")[-1].status == dc.FAIL


def test_the_requests_file_makes_the_demo_investors_play_and_the_pack_expects_the_category_after_the_game():
    requests_data = dc.load_requests()
    for key in ("saver", "balanced", "beginner"):
        game = requests_data["personas"][key]["body"]["answers"]["risk_game"]
        assert set(game) == {"tolerance", "loss_acceptance", "completed_at"} and game["completed_at"].startswith("2026-10-16")
    assert [PACK[k]["profile_with_game"]["c_level"] for k in ("saver", "balanced", "beginner")] == ["C2", "C3", "C4"]


def test_an_old_backend_fails_the_run_and_a_game_that_changes_nothing_fails_it_too(monkeypatch, capsys):
    code, out = run_against(FakeServer(knows_the_game=False), monkeypatch, capsys, "--quick")
    assert code == 1 and "HTTP 422: this backend does not know answers.risk_game" in out
    code, out = run_against(FakeServer(game_changes_nothing=True), monkeypatch, capsys, "--quick")
    assert code == 1 and "FAIL  beginner: POST /profile with the Risk Game" in out


def test_a_run_with_an_unusual_game_weight_warns_and_still_passes(monkeypatch, capsys):
    code, out = run_against(FakeServer(status={**GOOD_STATUS, "risk_game_weight": 0.25, "risk_game_may_raise": False}), monkeypatch, capsys, "--quick")
    assert code == 0 and "WARN  Risk Game weight is 0.3333" in out and "WARN  Risk Game may raise the category" in out
    assert "risk_game_weight=0.25; risk_game_may_raise=False" in out                                      # printed in the facts
