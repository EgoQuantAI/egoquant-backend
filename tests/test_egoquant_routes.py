"""Tests for the EgoQuant HTTP routes. SYNTHETIC prices (EGOQUANT_DATA=synthetic): no network, no keys.

Runs under both `python -m unittest discover -s tests` and `pytest`.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import egoquant_routes
from egoquant.agents.market_intel import PriceSourceError
from egoquant.demo.personas import PERSONAS
from egoquant.preferences import build_form, load_preference_bank, to_interest_fields
from egoquant.risk_profile import build_risk_profile
from tests.helpers import ASSESSED_AT, BANK, CONFIG

PREF_BANK = load_preference_bank()


def answers_body(persona) -> dict:
    a = persona.answers()
    return {"questionnaire_version": a.questionnaire_version, "choices": dict(a.choices),
            "multi_choices": {k: list(v) for k, v in a.multi_choices.items()},
            "amount_invested": a.amount_invested, "amount_needed": a.amount_needed}


def interest_areas(persona) -> list[str]:
    """The persona's preference-form answers turned into Agent C interest areas, as the pipeline does."""
    profile = build_risk_profile(persona.answers(), BANK, CONFIG, assessed_at=ASSESSED_AT)
    form = build_form(PREF_BANK, profile, CONFIG, list(persona.trading_platforms))
    return to_interest_fields(persona.preference_answers(), form, PREF_BANK)["interest_areas"]


def portfolio_body(persona, **overrides) -> dict:
    body = {"answers": answers_body(persona), "interest_areas": interest_areas(persona),
            "trading_platforms": list(persona.trading_platforms), "horizon_years": persona.horizon_years,
            "liquidity_need": persona.liquidity_need, "min_cash_buffer": persona.min_cash_buffer}
    body.update(overrides)
    return body


class EgoQuantRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(egoquant_routes.router)
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off"})   # no news feeds
        self.env.start()
        egoquant_routes._portfolio_timestamps.clear()
        egoquant_routes._debug_timestamps.clear()

    def tearDown(self):
        self.env.stop()

    # ---- questionnaire and options --------------------------------------------------------
    def test_questionnaire_hides_points_and_answer_key(self):
        data = self.client.get("/questionnaire").json()
        self.assertEqual(data["questionnaire_version"], egoquant_routes.BANK.questionnaire_version)
        self.assertEqual(len(data["questions"]), len(egoquant_routes.BANK.questions))
        self.assertEqual({s["id"] for s in data["sections"]}, {q["section"] for q in data["questions"]})
        for q in data["questions"]:
            self.assertEqual(set(q) - {"hint"}, {"id", "section", "type", "text", "options"})
            for option in q["options"]:
                self.assertEqual(set(option), {"id", "text"})

    def test_no_two_questions_read_the_same(self):
        """GL9/GL10 (the scale's gain/loss framing pair) read alike: each carries its own hint saying which it is."""
        data = self.client.get("/questionnaire").json()
        seen = [(q["text"], tuple(o["text"] for o in q["options"])) for q in data["questions"]]
        self.assertEqual(len(seen), len(set(seen)))
        by_id = {q["id"]: q for q in data["questions"]}
        self.assertIn("GAIN", by_id["GL9"]["hint"])
        self.assertIn("LOSS", by_id["GL10"]["hint"])
        self.assertTrue(all("gain" in o["text"] for o in by_id["GL9"]["options"]))
        self.assertTrue(all("lose" in o["text"] or "loss" in o["text"] for o in by_id["GL10"]["options"]))
        self.assertEqual({q["id"] for q in data["questions"] if "hint" in q}, {"GL9", "GL10"})

    def test_options_list_interests_platforms_and_game(self):
        data = self.client.get("/portfolio/options").json()
        self.assertIn("stocks", data["interest_areas"])
        self.assertIn("fidelity", [p["id"] for p in data["trading_platforms"]])
        self.assertEqual([g["choice"] for g in data["game_choices"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(data["data"], "synthetic")
        self.assertEqual(set(data["interest_products"]), set(data["interest_areas"]))
        self.assertIn("QQQ", [p["ticker"] for p in data["interest_products"]["technology"]])

    # ---- profile ------------------------------------------------------------------------
    def test_profile_matches_each_persona_class(self):
        for persona in PERSONAS.values():
            with self.subTest(persona=persona.key):
                response = self.client.post("/profile", json=answers_body(persona))
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data["c_level"], persona.expected_class)
                self.assertTrue(data["explanation"])
                self.assertEqual(data["c_level"], "C1" if data["no_loss_gate"] else
                                 f"C{min(data['tolerance_level'], data['capacity_level'])}")

    def test_profile_rejects_bad_answers(self):
        persona = next(iter(PERSONAS.values()))
        missing = answers_body(persona)
        del missing["choices"]["GL1"]
        self.assertEqual(self.client.post("/profile", json=missing).status_code, 422)
        too_much_needed = answers_body(persona) | {"amount_invested": 100, "amount_needed": 200}
        self.assertEqual(self.client.post("/profile", json=too_much_needed).status_code, 422)
        old_version = answers_body(persona) | {"questionnaire_version": "old"}
        self.assertEqual(self.client.post("/profile", json=old_version).status_code, 409)
        extra = answers_body(persona) | {"c_level": "C5"}
        self.assertEqual(self.client.post("/profile", json=extra).status_code, 422)

    # ---- portfolio ----------------------------------------------------------------------
    def test_every_persona_gets_an_approved_labeled_report(self):
        for persona in PERSONAS.values():
            with self.subTest(persona=persona.key):
                response = self.client.post("/portfolio", json=portfolio_body(persona))
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(data["approved"], data["violations"])
                self.assertEqual(data["c_level"], persona.expected_class)
                self.assertEqual(data["data_source"], "synthetic")
                self.assertEqual(data["report"]["risk_review_approved"], True)
                self.assertIn("<html", data["html"].lower())
                self.assertIn("synthetic", " ".join(data["report"]["banners"]))

    def test_market_briefing_is_attached_and_never_changes_the_portfolio(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        plain = self.client.post("/portfolio", json=portfolio_body(persona)).json()
        egoquant_routes._portfolio_timestamps.clear()
        with patch.dict(os.environ, {"EGOQUANT_BRIEFING": "fixture"}):          # DEMO sample: no network
            with_news = self.client.post("/portfolio", json=portfolio_body(persona)).json()
        self.assertIsNone(plain["report"]["market_briefing"])
        self.assertIsNotNone(with_news["report"]["market_briefing"])
        self.assertEqual(with_news["report"]["allocation"], plain["report"]["allocation"])
        self.assertEqual(with_news["report"]["equity_weight"], plain["report"]["equity_weight"])
        self.assertEqual(with_news["approved"], plain["approved"])

    def test_debug_route_has_its_own_rate_limit(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C3")
        egoquant_routes._debug_timestamps.extend([egoquant_routes.time.monotonic()] * egoquant_routes._DEBUG_LIMIT)
        self.assertEqual(self.client.post("/debug/portfolio", json={"c_level": "C3"}).status_code, 429)
        self.assertEqual(self.client.post("/portfolio", json=portfolio_body(persona)).status_code, 200)

    def test_cautious_game_tightens_the_portfolio_through_agent_b(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        game = {"choice": 1, "played_at": "2026-10-17T10:00:00"}
        without = self.client.post("/portfolio", json=portfolio_body(persona)).json()
        with_game = self.client.post("/portfolio", json=portfolio_body(persona, game=game)).json()
        self.assertTrue(with_game["approved"], with_game["violations"])
        self.assertTrue(any("behavioural" in c["name"] for c in with_game["report"]["limits"]))
        self.assertLessEqual(with_game["report"]["equity_weight"], without["report"]["equity_weight"] + 1e-9)

    def test_loss_acceptance_can_only_tighten_through_agent_b(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        def game(loss_acceptance):                       # bold overall (no limit from tolerance alone)
            g = {"tolerance": 100, "completed_at": "2026-10-17T10:00:00"}
            return g if loss_acceptance is None else g | {"loss_acceptance": loss_acceptance}
        def post(loss_acceptance):
            egoquant_routes._portfolio_timestamps.clear()
            return self.client.post("/portfolio", json=portfolio_body(persona, risk_game=game(loss_acceptance))).json()
        behavioural = lambda d: [c for c in d["report"]["limits"] if "behavioural" in c["name"]]
        # The game's two scores as Agent B's own tightening evidence: the reading that EGOQUANT_RISK_GAME_WEIGHT=0 (the kill switch)
        # restores. By default the game sits inside the risk category and Agent B does not count it a second time (below).
        with patch.dict(os.environ, {"EGOQUANT_RISK_GAME_WEIGHT": "0"}):
            no_score, neutral, retreats = post(None), post(50), post(0)
        self.assertEqual(behavioural(no_score), [])
        self.assertEqual(neutral["report"]["allocation"], no_score["report"]["allocation"])   # 50 = no signal
        self.assertTrue(retreats["approved"], retreats["violations"])
        self.assertTrue(behavioural(retreats))
        self.assertLessEqual(retreats["report"]["equity_weight"], no_score["report"]["equity_weight"] + 1e-9)
        self.assertIn("Loss acceptance 0/100", retreats["game_note"])
        counted_once = post(0)                                  # default: inside the category, never a second tightening
        self.assertIn("Risk Game played", counted_once["game_note"])
        self.assertEqual(behavioural(counted_once), [])

    def test_crash_test_sale_tightens_and_holding_on_changes_nothing(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        def post(crash):
            egoquant_routes._portfolio_timestamps.clear()
            game = {"tolerance": 100, "completed_at": "2026-10-17T10:00:00"}       # bold: no limit from tolerance
            return self.client.post("/portfolio", json=portfolio_body(
                persona, risk_game=game | ({} if crash is None else {"crash_test": crash})))
        plain = post(None).json()
        held = post({"sold_fraction": 0, "max_drawdown_seen": 0.35}).json()
        sold = post({"sold_at_drawdown": 0.22, "sold_fraction": 1, "max_drawdown_seen": 0.22}).json()
        behavioural = lambda d: [c for c in d["report"]["limits"] if "behavioural" in c["name"]]
        self.assertEqual(held["report"]["allocation"], plain["report"]["allocation"])
        self.assertIn("held on through a 35% fall", held["game_note"])
        self.assertTrue(sold["approved"], sold["violations"])
        self.assertTrue(behavioural(sold))
        self.assertLessEqual(sold["report"]["equity_weight"], plain["report"]["equity_weight"] + 1e-9)
        self.assertIn("sold 100% of the fund when it was down 22%", sold["game_note"])
        bad = post({"sold_fraction": 0.5, "max_drawdown_seen": 0.3})              # sold, but at what loss?
        self.assertEqual(bad.status_code, 422)
        self.assertIn("crash_test", bad.json()["detail"])

    def test_risk_game_score_maps_to_a_choice_and_tightens(self):
        self.assertEqual([egoquant_routes.risk_game_choice(t) for t in (0, 16, 17, 33, 34, 50, 51, 66, 67, 83, 84, 100)],
                         [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6])
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        cautious = {"tolerance": 5, "loss_acceptance": 20, "completed_at": "2026-10-17T10:00:00"}
        with patch.dict(os.environ, {"EGOQUANT_RISK_GAME_WEIGHT": "0"}):        # the kill switch: Agent B reads the game alone, as before
            data = self.client.post("/portfolio", json=portfolio_body(persona, risk_game=cautious)).json()
        self.assertTrue(data["approved"], data["violations"])
        self.assertIn("gamble 1 of 6", data["game_note"])
        self.assertTrue(any("behavioural" in c["name"] for c in data["report"]["limits"]))
        egoquant_routes._portfolio_timestamps.clear()
        inside = self.client.post("/portfolio", json=portfolio_body(persona, risk_game=cautious)).json()     # default: in the category
        self.assertTrue(inside["approved"], inside["violations"])
        self.assertIn("Risk Game played", inside["game_note"])
        self.assertIn("category_basis", inside["report"])
        self.assertFalse(any("behavioural" in c["name"] for c in inside["report"]["limits"]))
        both = portfolio_body(persona, risk_game=cautious, game={"choice": 2, "played_at": "2026-10-17T10:00:00"})
        self.assertEqual(self.client.post("/portfolio", json=both).status_code, 422)
        self.assertTrue(self.client.post("/portfolio", json=portfolio_body(persona)).json()["game_note"]
                        .startswith("Risk Game: not played"))

    def test_goal_path_returns_the_simulation_and_shortlist(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        goal = {"goal_amount": persona.amount_invested * 1.5, "monthly_flow": 100}
        data = self.client.post("/portfolio", json=portfolio_body(persona, **goal)).json()
        self.assertTrue(data["approved"], data["violations"])
        g = data["goal"]
        self.assertEqual(g["spec"]["start_amount"], persona.amount_invested)
        self.assertTrue(1 <= len(g["shortlist"]) <= 20)
        self.assertEqual(sum(s["chosen"] for s in g["shortlist"]), 1)
        self.assertTrue(g["shortlist"][0]["chosen"])                    # best first
        self.assertEqual(g["screening"]["counts"]["approved"], len(g["shortlist"]))
        self.assertTrue(0 <= g["chosen"]["prob_goal"] <= 1)
        self.assertEqual(g["fan"][0]["year"], 0)
        self.assertIn("not a forecast", g["label"])
        self.assertIsNone(self.client.post("/portfolio", json=portfolio_body(persona)).json()["goal"])
        bad = portfolio_body(persona, goal_amount=-5)
        self.assertEqual(self.client.post("/portfolio", json=bad).status_code, 422)

    def test_portfolio_rejects_bad_intake(self):
        persona = next(iter(PERSONAS.values()))
        cases = [
            portfolio_body(persona, interest_areas=["lottery"]),
            portfolio_body(persona, interest_areas=[]),
            portfolio_body(persona, trading_platforms=["my_bank"]),
            portfolio_body(persona, horizon_years=0),
            portfolio_body(persona, game={"choice": 7, "played_at": "2026-10-17T10:00:00"}),
            portfolio_body(persona, risk_class="C5"),
        ]
        for body in cases:
            with self.subTest(body=str(body)[-80:]):
                self.assertEqual(self.client.post("/portfolio", json=body).status_code, 422)

    # ---- investment preferences -> Agent C ----------------------------------------------
    def test_preference_form_offers_only_what_the_profile_allows(self):
        for persona in PERSONAS.values():
            with self.subTest(persona=persona.key):
                response = self.client.post("/preferences/form", json={
                    "answers": answers_body(persona), "trading_platforms": list(persona.trading_platforms)})
                self.assertEqual(response.status_code, 200, response.text)
                form = response.json()
                self.assertEqual(form["c_level"], persona.expected_class)
                p1 = next(q for q in form["questions"] if q["id"] == "P1")
                offered = {o["id"]: o for o in p1["options"]}
                self.assertTrue(offered["cash"]["available"])          # every profile may hold cash
                if persona.expected_class in ("C1", "C2"):
                    self.assertFalse(offered["crypto"]["available"])
                    self.assertTrue(offered["crypto"]["reason"])
        bad = {"answers": answers_body(next(iter(PERSONAS.values()))), "trading_platforms": ["my_bank"]}
        self.assertEqual(self.client.post("/preferences/form", json=bad).status_code, 422)

    def test_portfolio_from_preference_answers_reports_what_agent_c_priced(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")
        body = portfolio_body(persona, preferences=persona.preference_answers().model_dump())
        del body["interest_areas"]
        data = self.client.post("/portfolio", json=body).json()
        self.assertTrue(data["approved"], data["violations"])
        universe = data["universe"]
        self.assertTrue(universe["instruments"] and universe["interest_areas"])
        self.assertEqual({i["ticker"] for i in universe["instruments"]} & {"BIL", "AGG"}, {"BIL", "AGG"})
        self.assertEqual(universe["new_requests"], 0)                   # synthetic prices cost no requests

    def test_portfolio_preferences_are_checked_against_the_profile(self):
        persona = next(p for p in PERSONAS.values() if p.expected_class == "C1")
        version = egoquant_routes.PREF_BANK.preferences_version
        crypto = portfolio_body(persona, preferences={"preferences_version": version, "choices": {"P1": ["crypto"]}})
        del crypto["interest_areas"]
        response = self.client.post("/portfolio", json=crypto)
        self.assertEqual(response.status_code, 422)
        self.assertIn("not available", response.json()["detail"])
        old = portfolio_body(persona, preferences={"preferences_version": "old", "choices": {"P1": ["cash"]}})
        del old["interest_areas"]
        self.assertEqual(self.client.post("/portfolio", json=old).status_code, 409)
        both = portfolio_body(persona, preferences={"preferences_version": version, "choices": {"P1": ["cash"]}})
        self.assertEqual(self.client.post("/portfolio", json=both).status_code, 422)
        neither = portfolio_body(persona)
        del neither["interest_areas"]
        self.assertEqual(self.client.post("/portfolio", json=neither).status_code, 422)

    def test_market_data_failure_is_a_clean_503(self):
        persona = next(iter(PERSONAS.values()))
        with patch("egoquant_routes.run_pipeline", side_effect=PriceSourceError("secret-vendor-detail")):
            response = self.client.post("/portfolio", json=portfolio_body(persona))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-vendor-detail", response.text)

    def test_portfolio_rate_limit(self):
        persona = next(iter(PERSONAS.values()))
        with patch.object(egoquant_routes, "_PORTFOLIO_LIMIT", 1):
            self.assertEqual(self.client.post("/portfolio", json=portfolio_body(persona)).status_code, 200)
            self.assertEqual(self.client.post("/portfolio", json=portfolio_body(persona)).status_code, 429)


IBKR_DATA = Path(__file__).parent / "agent_b" / "data"
FAKE_TOKEN = "FAKEtoken0123456789"                    # invented; must never appear in any response


class IbkrRouteTest(unittest.TestCase):
    """The investor's IBKR trading history -> Agent B. Invented sample reports only; IBKR's server is mocked."""

    def setUp(self):
        app = FastAPI()
        app.include_router(egoquant_routes.router)
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {"EGOQUANT_DATA": "synthetic", "EGOQUANT_BRIEFING": "off"})
        self.env.start()
        egoquant_routes._portfolio_timestamps.clear()
        self.persona = next(p for p in PERSONAS.values() if p.expected_class == "C4")

    def tearDown(self):
        self.env.stop()

    def post(self, **kw):
        egoquant_routes._portfolio_timestamps.clear()
        return self.client.post("/portfolio", json=portfolio_body(self.persona, **kw))

    def report(self, name):
        return (IBKR_DATA / name).read_text(encoding="utf-8")

    def test_an_uploaded_report_feeds_agent_b_and_the_current_holdings(self):
        plain = self.post().json()
        seller = self.post(ibkr_report=self.report("ibkr_cautious_seller.xml"))
        holder = self.post(ibkr_report=self.report("ibkr_steady_holder.csv")).json()
        self.assertEqual(seller.status_code, 200, seller.text)
        data = seller.json()
        behavioural = lambda d: [c for c in d["report"]["limits"] if "behavioural" in c["name"]]
        self.assertTrue(data["approved"], data["violations"])
        self.assertTrue(behavioural(data))                              # sold in the falls -> a loss limit
        self.assertLessEqual(data["report"]["equity_weight"], plain["report"]["equity_weight"] + 1e-9)
        self.assertEqual(behavioural(holder), [])                         # held on -> no limit
        summary = data["trading_history"]
        self.assertEqual((summary["source"], summary["currency"]), ("ibkr_report", "USD"))
        self.assertGreater(summary["n_trades"], 0)
        self.assertTrue(summary["notes"])                                 # trade-price valuation is labeled
        self.assertNotIn("U0000000", seller.text)                         # the account id is never returned
        self.assertNotIn("trades", summary)                               # counts and dates, never raw trades

    def test_a_flex_token_is_used_once_and_never_returned(self):
        calls = []

        def fake_fetch(token, query_id, **_):
            calls.append((token, query_id))
            return self.report("ibkr_cautious_seller.xml")

        with patch("egoquant.accounts.ibkr_input.fetch_flex_report", fake_fetch):
            response = self.post(ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [(FAKE_TOKEN, "123456")])
        self.assertEqual(response.json()["trading_history"]["source"], "ibkr_flex")
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_ibkr_errors_are_user_safe_and_never_echo_the_token(self):
        from egoquant.agents.behavioral.ibkr_flex import FlexError

        def expired(*_, **__):
            raise FlexError("token_expired", "Your Flex token has expired. Create a new one in IBKR.")

        with patch("egoquant.accounts.ibkr_input.fetch_flex_report", expired):
            response = self.post(ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("expired", response.json()["detail"])
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_bad_input_is_refused_before_any_market_data(self):
        both = self.post(ibkr_report=self.report("ibkr_steady_holder.csv"),
                         ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(both.status_code, 422)
        junk = self.post(ibkr_report="<html><script>alert(1)</script></html>")
        self.assertEqual(junk.status_code, 422)
        self.assertIn("IBKR report", junk.json()["detail"])
        self.assertNotIn("<script>", junk.json()["detail"])               # the message never repeats the file
        renamed = self.post(ibkr_report=self.report("ibkr_steady_holder.csv"), ibkr_filename="statement.pdf")
        self.assertEqual(renamed.status_code, 422)                       # the name must match the content
        self.assertEqual(self.post(ibkr_report=self.report("ibkr_steady_holder.csv"),
                                   ibkr_filename="trades.csv").status_code, 200)
        bad_query = self.post(ibkr_flex={"token": FAKE_TOKEN, "query_id": "12; DROP"})
        self.assertEqual(bad_query.status_code, 422)

    def check(self, **body):
        egoquant_routes._ibkr_check_timestamps.clear()
        return self.client.post("/portfolio/ibkr-check", json=body)

    def test_the_ibkr_check_says_what_is_in_the_report_without_returning_it(self):
        ok = self.check(ibkr_report=self.report("ibkr_cautious_seller.xml"), ibkr_filename="trades.xml")
        self.assertEqual(ok.status_code, 200, ok.text)
        data = ok.json()
        self.assertEqual((data["source"], data["currency"]), ("ibkr_report", "USD"))
        self.assertGreater(data["n_trades"], 0)
        self.assertLessEqual(data["start"], data["end"])
        self.assertNotIn("U0000000", ok.text)                              # never the account id
        self.assertEqual(set(data), {"source", "n_trades", "n_cash_flows", "start", "end", "currency", "notes"})

    def test_the_ibkr_check_catches_a_bad_file_or_token_before_the_build(self):
        from egoquant.agents.behavioral.ibkr_flex import FlexError

        def expired(*_, **__):
            raise FlexError("token_expired", "Your Flex token has expired. Create a new one in IBKR.")

        with patch("egoquant.accounts.ibkr_input.fetch_flex_report", expired):
            bad_token = self.check(ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(bad_token.status_code, 422)
        self.assertIn("expired", bad_token.json()["detail"])
        self.assertNotIn(FAKE_TOKEN, bad_token.text)
        junk = self.check(ibkr_report="<html><script>alert(1)</script></html>")
        self.assertEqual(junk.status_code, 422)
        self.assertNotIn("<script>", junk.text)
        self.assertEqual(self.check().status_code, 422)                    # neither
        both = self.check(ibkr_report=self.report("ibkr_steady_holder.csv"),
                          ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(both.status_code, 422)
        self.assertNotIn(FAKE_TOKEN, both.text)
        self.assertEqual(self.check(ibkr_flex={"token": "short", "query_id": "123456"}).status_code, 422)

    def test_the_ibkr_check_fetches_with_the_token_and_never_returns_it(self):
        calls = []

        def fake_fetch(token, query_id, **_):
            calls.append((token, query_id))
            return self.report("ibkr_cautious_seller.xml")

        with patch("egoquant.accounts.ibkr_input.fetch_flex_report", fake_fetch):
            response = self.check(ibkr_flex={"token": FAKE_TOKEN, "query_id": "123456"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [(FAKE_TOKEN, "123456")])
        self.assertEqual(response.json()["source"], "ibkr_flex")
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_the_ibkr_check_has_its_own_rate_limit(self):
        egoquant_routes._ibkr_check_timestamps.clear()
        body = {"ibkr_report": self.report("ibkr_steady_holder.csv")}
        with patch.object(egoquant_routes, "_IBKR_CHECK_LIMIT", 1):
            self.assertEqual(self.client.post("/portfolio/ibkr-check", json=body).status_code, 200)
            self.assertEqual(self.client.post("/portfolio/ibkr-check", json=body).status_code, 429)
        egoquant_routes._ibkr_check_timestamps.clear()

    def test_final_positions_become_weights_on_catalog_tickers_with_cash_as_t_bills(self):
        from types import SimpleNamespace
        history = SimpleNamespace(final_weights={"VTI": 0.5, "CASH": 0.3, "NOTLISTED": 0.2})
        weights, left_out = egoquant_routes._ibkr_holdings(history, {"VTI", "BIL"})
        self.assertEqual(left_out, ["NOTLISTED"])
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertAlmostEqual(weights["BIL"], 0.3 / 0.8)
        self.assertEqual(egoquant_routes._ibkr_holdings(SimpleNamespace(final_weights={}), {"VTI"}), (None, []))


class DirectoryLoadTest(unittest.TestCase):
    """Agent C's directory is loaded in the background; no request ever waits for the build."""

    def setUp(self):
        self.saved = {k: getattr(egoquant_routes, k) for k in
                      ("_directory", "_directory_checked_on", "_directory_attempt_at", "_directory_loading")}
        egoquant_routes._directory, egoquant_routes._directory_checked_on = None, None
        egoquant_routes._directory_attempt_at, egoquant_routes._directory_loading = None, False

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(egoquant_routes, k, v)

    def _wait(self):
        for thread in __import__("threading").enumerate():
            if thread.name == "agent-c-directory":
                thread.join(5)

    def test_first_request_gets_the_curated_catalog_and_later_ones_the_directory(self):
        directory, calls = object(), []
        with patch.object(egoquant_routes, "load_directory", lambda: calls.append(1) or directory):
            self.assertIsNone(egoquant_routes._directory_for_request())       # not ready: curated catalog
            self._wait()
            self.assertIs(egoquant_routes._directory_for_request(), directory)
            self.assertIs(egoquant_routes._directory_for_request(), directory)
        self.assertEqual(len(calls), 1)                                       # loaded once per day

    def test_a_failed_load_is_not_retried_on_every_request(self):
        calls = []
        with patch.object(egoquant_routes, "load_directory", lambda: calls.append(1) or None):
            egoquant_routes._directory_for_request()
            self._wait()
            self.assertIsNone(egoquant_routes._directory_for_request())
            self._wait()
        self.assertEqual(len(calls), 1)

    def test_live_portfolio_passes_the_directory_it_has_and_never_builds_it_inline(self):
        app = FastAPI()
        app.include_router(egoquant_routes.router)
        client, seen = TestClient(app), {}
        real = egoquant_routes.run_pipeline

        def capture(*args, **kw):                      # record what the route passed, then run offline
            seen.update(kw)
            return real(*args, **{**kw, "price_source": egoquant_routes.SyntheticPriceSource()})

        persona = next(p for p in PERSONAS.values() if p.expected_class == "C3")
        egoquant_routes._portfolio_timestamps.clear()
        with patch.dict(os.environ, {"EGOQUANT_DATA": "", "EGOQUANT_BRIEFING": "off"}),                 patch.object(egoquant_routes, "run_pipeline", capture),                 patch.object(egoquant_routes, "load_directory", lambda: None):
            self.assertEqual(client.post("/portfolio", json=portfolio_body(persona)).status_code, 200)
        self.assertIsNone(seen["directory"])
        self._wait()


class FallbackLabelTest(unittest.TestCase):
    def test_fallback_reason_is_a_safe_label_never_the_vendor_text(self):
        from types import SimpleNamespace
        label = egoquant_routes._fallback_label
        self.assertIsNone(label(None))
        self.assertIsNone(label(SimpleNamespace(fallback_reason=None)))
        self.assertEqual(label(SimpleNamespace(fallback_reason="Tiingo refused the API key (401): secret body")),
                         "Tiingo key rejected")
        self.assertEqual(label(SimpleNamespace(fallback_reason="something odd")), "data vendor error")


if __name__ == "__main__":
    unittest.main()
