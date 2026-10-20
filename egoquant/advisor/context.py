"""Engine facts for the chat, and the seal that lets the stateless service trust them later.

The service keeps no state, so the browser stores the portfolio result and sends it back with each chat
message. A browser copy could be edited, so /portfolio also returns `chat_context`: the compact facts the
advisor needs, sealed with an HMAC. The chat accepts portfolio facts only with a valid seal. The risk
profile needs no seal: the chat recomputes it from the answers with the same deterministic scoring.

Facts are data minimised: no investor id, no questionnaire answers or points. The score appears only inside
the plain-language explanation the Profile page already shows the investor.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import zlib

logger = logging.getLogger(__name__)

CONTEXT_VERSION = 1
MAX_AGE_SECONDS = 14 * 24 * 3600          # PROVISIONAL: a portfolio older than this is rebuilt, not discussed
MAX_TOKEN_LENGTH = 120_000

# Size caps keep the prompt short (cost, latency) and bound what a long briefing can push into it.
MAX_HOLDINGS = 40
MAX_BRIEFING_ITEMS = 8
MAX_TEXT = 400


class ContextError(ValueError):
    """The sealed context is missing its seal, was changed, is too old or is unreadable."""


# --------------------------------------------------------------------------
# Seal
# --------------------------------------------------------------------------
_process_secret = secrets.token_bytes(32)


def _secret() -> bytes:
    """EGOQUANT_CONTEXT_SECRET if set. Otherwise a key derived from the OpenAI key (stable across restarts,
    never the key itself), and as a last resort a per-process random key (contexts die with the process)."""
    explicit = os.getenv("EGOQUANT_CONTEXT_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        return hmac.new(openai_key.encode(), b"egoquant-chat-context-v1", hashlib.sha256).digest()
    return _process_secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal_context(facts: dict, now: float | None = None) -> str:
    payload = {"v": CONTEXT_VERSION, "iat": int(now if now is not None else time.time()), "facts": facts}
    body = _b64(zlib.compress(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), 9))
    signature = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def open_context(token: str, now: float | None = None) -> dict:
    """The facts inside a sealed context. Raises ContextError on any doubt."""
    if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 1:
        raise ContextError("malformed")
    body, signature = token.split(".")
    expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise ContextError("bad seal")
    try:
        payload = json.loads(zlib.decompress(_unb64(body), bufsize=MAX_TOKEN_LENGTH * 8))
    except (ValueError, zlib.error):
        raise ContextError("unreadable") from None
    if payload.get("v") != CONTEXT_VERSION:
        raise ContextError("old version")
    age = (now if now is not None else time.time()) - payload.get("iat", 0)
    if age > MAX_AGE_SECONDS or age < -600:
        raise ContextError("expired")
    return payload["facts"]


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------
def _clip(text: str | None, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _r(x, digits: int = 4):
    return round(x, digits) if isinstance(x, (int, float)) else x


def build_profile_facts(risk, explanation: list[str], blocked_product_types: list[str]) -> dict:
    """The investor's risk profile as the advisor may discuss it (the C-level, its reasons and limits)."""
    facts = {
        "c_level": risk.c_level,
        "binding_dimension": risk.binding_dimension,          # which side set the C-level
        "tolerance_level": risk.tolerance_level,              # 1-5 bands, not the raw score
        "capacity_level": risk.capacity_level,
        "no_loss_gate": risk.no_loss_gate,
        "max_equity_share": _r(risk.suitability_max_equity),
        "max_loss_capacity": _r(risk.loss_capacity),
        "max_product_risk_level": f"R{risk.max_r_level}",
        "product_access": risk.product_access,
        "blocked_product_types": list(blocked_product_types),
        "financial_knowledge_correct_of_3": risk.knowledge_score,
        "flags": list(risk.flags),
        "explanation": [_clip(line) for line in explanation],
    }
    if getattr(risk, "game_played", False):                    # docs/RISK_GAME_BLEND.md: only when the game was played
        facts["risk_game"] = {"played": True, "game_level": risk.game_level, "blended_tolerance_level": risk.blended_level,
                              "category_without_the_game": risk.questionnaire_class,
                              "weights": "two thirds questionnaire tolerance, one third Risk Game"}
    return facts


def build_portfolio_facts(out: dict) -> dict:
    """Compact facts out of one /portfolio response (PortfolioOut as a dict, before `chat_context`)."""
    report = out.get("report") or {}
    facts: dict = {
        "approved_by_risk_engine": out.get("approved"),
        "optimizer_status": out.get("status"),
        "c_level": out.get("c_level"),
        "prices_as_of": str(out.get("prices_as_of") or out.get("as_of")),   # the snapshot's day when live prices failed
        "price_note": out.get("price_note"),                                # None unless the saved snapshot was used
        "data_source": out.get("data_source"),
        "fallback_reason": out.get("fallback_reason"),
        "risk_engine_violations": [_clip(v) for v in out.get("violations") or []],
        "risk_engine_checks": [{"name": c.get("name"), "passed": c.get("passed"), "detail": _clip(c.get("detail"))}
                               for c in out.get("checks") or []],
        "risk_game_note": out.get("game_note"),
        "currency": report.get("currency"),
        "reference_amount": report.get("reference_amount"),
        "headline": _clip(report.get("headline")),
        "class_weights": {k: _r(report.get(f"{k}_weight", 0.0))
                          for k in ("equity", "bond", "cash", "commodity", "crypto")},
        "expected_volatility_annual": _r(report.get("ex_ante_vol")),
        "worst_hypothetical_loss": _r(report.get("worst_loss")),
        "worst_scenario": report.get("worst_scenario"),
        "loss_limit_scenario": report.get("limit_scenario"),
        "ongoing_fee_annual": _r(report.get("ongoing_fee")),
        "holdings": [{"ticker": a.get("asset_id"), "name": _clip(a.get("name"), 80), "class": a.get("asset_class"),
                      "weight": _r(a.get("weight")), "amount": _r(a.get("amount"), 2),
                      "fee": _r(a.get("expense_ratio")), "risk_share": _r(a.get("risk_share"))}
                     for a in (report.get("allocation") or [])[:MAX_HOLDINGS]],
        "limits": [{"name": lc.get("name"), "value": _r(lc.get("value")), "limit": _r(lc.get("limit")),
                    "direction": lc.get("direction"), "ok": lc.get("ok"), "source": lc.get("source")}
                   for lc in report.get("limits") or []],
        "stress_tests_hypothetical": [{"name": s.get("name"), "description": _clip(s.get("description"), 200),
                                       "portfolio_return": _r(s.get("portfolio_return")),
                                       "amount_change": _r(s.get("amount_change"), 2)}
                                      for s in report.get("stress") or []],
        "reasons": [{"kind": x.get("kind"), "text": _clip(x.get("text"))} for x in report.get("reasons") or []],
        "interest_notes": [_clip(n) for n in report.get("interest_notes") or []],
        "banners": [_clip(b) for b in report.get("banners") or []],
        "profile_valid_until": report.get("profile_valid_until"),
        "profile_expired": report.get("profile_expired"),
    }
    if report.get("category_basis"):                           # only when the Risk Game was played
        facts["category_basis"] = {k: v for k, v in report["category_basis"].items()}
    if report.get("trades"):
        facts["trades_from_current_holdings"] = [
            {"ticker": t.get("asset_id"), "current": _r(t.get("current")), "proposed": _r(t.get("proposed"))}
            for t in report["trades"][:MAX_HOLDINGS]]
    if out.get("goal"):
        facts["goal"] = _goal_facts(out["goal"], report.get("goal"))
    if out.get("universe"):
        u = out["universe"]
        facts["universe"] = {
            "interest_areas": u.get("interest_areas"),
            "n_priced": len(u.get("instruments") or []),
            "not_priced_examples": [{"ticker": e.get("ticker"), "reason": _clip(e.get("reason"), 160)}
                                    for e in (u.get("not_fetched") or [])[:8]],
            "n_not_priced": u.get("n_not_fetched"),
            "watchlist": (u.get("watchlist") or [])[:10],
        }
    if report.get("what_changed"):
        # Agent C's drawdowns and Agent D's tone alerts; the only action is offering a risk-profile re-check
        facts["what_changed"] = [_clip(x) for x in report["what_changed"][:10]]
        facts["what_changed_action"] = report.get("what_changed_action")
    if report.get("market_briefing"):
        facts["market_briefing"] = _briefing_facts(report["market_briefing"])
    if report.get("market_briefing_month"):
        # the same news summarised by month (the News tab's toggle); headlines are already in the weekly facts
        facts["market_briefing_month"] = _briefing_facts(report["market_briefing_month"], with_items=False)
        facts["what_changed_month"] = [_clip(x) for x in (report.get("what_changed_month") or [])[:10]]
    return facts


def _goal_facts(g: dict, gr: dict | None = None) -> dict:
    """The simulation's numbers (_goal_summary) plus the report's own wording of it (report.goal), so the
    Advisor uses the same rounded chance, frequency phrasing and levers the investor sees on the page."""
    chosen = g.get("chosen") or {}
    gr = gr or {}
    if gr.get("levers"):
        levers = [{"change": _clip(lv.get("change"), 160), "chance": lv.get("chance"), "prob_goal": _r(lv.get("prob_goal"))}
                  for lv in gr["levers"][:8]]
    else:                                         # numbers only (report without the goal section)
        levers = [{"kind": lv.get("kind"), "value": _r(lv.get("value"), 2), "prob_goal": _r(lv.get("prob_goal"))}
                  for lv in (g.get("levers") or [])[:8]]
    shown = {
        "chance_as_shown": gr.get("chance"),
        "headline": _clip(gr.get("headline")),
        "outcomes_by_frequency": [{"label": x.get("label"), "value": x.get("value")} for x in gr.get("outcomes") or []],
        "how_bad_a_miss_could_be": [{"label": x.get("label"), "value": x.get("value"), "text": _clip(x.get("text"), 300)}
                                    for x in gr.get("severity") or []],
        "method_steps": [{"title": s.get("title"), "text": _clip(s.get("text"), 500)} for s in gr.get("method") or []],
    } if gr else {}
    regime = _regime_facts(g.get("sampler"))
    facts = {
        **{k: v for k, v in shown.items() if v},
        # What would change the odds: ONE input changed, re-simulated with the same portfolio on the same paths.
        "levers_what_would_change_the_odds": levers,
        "uncertainty": {
            "sampling_error": _r(g.get("std_error")),
            "by_history_half": [{"from": c.get("start"), "to": c.get("end"), "prob_goal": _r(c.get("prob_goal"))}
                                for c in (g.get("period_checks") or [])[:2]],
            "precision_note": _clip(gr.get("precision")) if gr else None,
        },
        "spec": g.get("spec"),
        "simulation": {"paths": g.get("n_paths"), "history_start": g.get("history_start"),
                       "history_end": g.get("history_end"),
                       "method": "regime-aware block bootstrap of past daily returns (in-sample)" if regime
                       else "block bootstrap of past daily returns (in-sample)"},
        "chosen_reason": _clip(g.get("chosen_reason")),
        "chosen": {k: _r(chosen.get(k)) for k in ("prob_goal", "p10", "p50", "p90", "worst5_shortfall",
                                                   "prob_depleted", "drawdown_p50", "drawdown_p90",
                                                   "prob_drawdown_20", "equity_weight") if k in chosen},
        "total_contributed": _r(g.get("total_contributed"), 2),
        "shortlist": [{"label": o.get("label"), "equity_weight": _r(o.get("equity_weight")),
                       "prob_goal": _r(o.get("prob_goal")), "p50": _r(o.get("p50"), 0),
                       "vol": _r(o.get("vol")), "stress_loss": _r(o.get("stress_loss")), "chosen": o.get("chosen")}
                      for o in (g.get("shortlist") or [])[:8]],
        "behavioral_screening_facts": [_clip(f) for f in ((g.get("screening") or {}).get("facts") or [])[:6]],
    }
    if regime:
        facts["market_regime"] = regime
    return facts


def _open_chance(p):
    """A chance rounded to 4 digits, or, within half a point of 0% or 100%, the report's own words ("less than 1%",
    "more than 99%"): number grounding accepts a claim within 0.55 points, so 0.9999 would support "100%"."""
    if p is None:
        return None
    p = float(p)
    if 0.0 < p < 0.005 or 0.995 < p < 1.0:
        from egoquant.report import _chance
        return _chance(p)
    return _r(p)


def _regime_facts(rec: dict | None) -> dict | None:
    """Only when the regime-aware sampler ran: the model and the regime facts the report shows. Keys with
    "probability" / "in_100" are chance-type values for number grounding."""
    from egoquant.regime.hook import is_regime
    from egoquant.report import _chance
    if not is_regime(rec):
        return None
    names = rec.get("state_names") or ["calm", "stressed", "crisis"]
    today = float(rec.get("regime_today_probability") or 0.0)
    funds = rec.get("model_funds") or ["SPY"]
    on = "the S&P 500 fund SPY" if funds == ["SPY"] else "the funds " + " and ".join(funds)
    return {
        "model": f"{len(names)}-state Gaussian Hidden Markov Model (unsupervised machine learning, not an LLM) fitted "
                 f"offline on the daily returns and 21-day volatility of {on}",
        "fit_period": f"{rec.get('fit_start')} to {rec.get('fit_end')}",
        "how_it_changes_the_simulation": "each simulated month is drawn only from past months that began in the same "
                                         "regime, and the regime moves month by month with the learned chances, so "
                                         "crises last as long as they did in the past",
        "regime_today": rec.get("regime_today"),
        "regime_today_probability": _open_chance(today),
        "regime_today_probability_as_shown": _chance(today),
        "regime_probabilities_today": {n: _open_chance(p) for n, p in zip(names, rec.get("start_probs") or [])},
        "long_crisis_in_100": rec.get("long_crisis_in_100"),
        "long_crisis_probability": _r(rec.get("long_crisis_share")),
        "long_crisis_definition": "at least one crisis period lasting 3 months or more in a simulated future",
    }


def _coverage(a: dict) -> str:
    """The card status in words, the same three states the News tab shows."""
    c = a.get("coverage_status") or {}
    state = c.get("state")
    if state == "not_enough_coverage":
        return f"not enough coverage ({c.get('n', 0)} headlines; needs {c.get('needed', 3)})"
    if state == "not_yet_assessed":
        return f"not yet assessed ({c.get('n_waiting', 0)} headlines waiting)"
    return "tone" if state == "tone" else ""


def _card(a: dict) -> dict:
    return {"subject": a.get("subject"), "name": _clip(a.get("name"), 80), "tone": _r(a.get("tone"), 2),
            "tone_previous": _r(a.get("tone_previous"), 2), "change": _r(a.get("change"), 2),
            "coverage": _coverage(a), "text": _clip(a.get("text"), 200)}


def _briefing_facts(b: dict, with_items: bool = True) -> dict:
    """Agent D's briefing in the News tab's structure: holdings (weight > 0), the areas the investor follows,
    source tiers and tone methods. Article titles and summaries are third-party text: the prompt marks them as
    untrusted data."""
    insts = b.get("instrument_aggregates") or []
    held = [a for a in insts if a.get("held")]
    if not held and not any("held" in a for a in insts):            # older briefings without the UI fields
        held = insts
    facts = {
        "period": f"{b.get('briefing_period') or 'week'}: {b.get('period_start')} to {b.get('period_end')}",
        "data_source": b.get("data_source"),
        "tone_note": _clip(b.get("tone_note"), 300),                 # how tone is assessed, and how well
        "your_holdings": [{**_card(a), "weight": _r(a.get("weight"))} for a in held[:12]],
        "areas_you_follow": [{**_card(a), "parent_area": a.get("parent_area"),
                              "instruments": [{"ticker": i.get("ticker"), "status": i.get("status"),
                                               "r_level": i.get("r_level")} for i in (a.get("instruments") or [])[:8]]}
                             for a in (b.get("area_aggregates") or [])[:8]],
        "alerts": [{"subject": a.get("subject"), "tone": _r(a.get("tone"), 2),
                    "tone_previous": _r(a.get("tone_previous"), 2), "n_items": a.get("n_items"),
                    "text": _clip(a.get("summary") or a.get("message"), 240)}
                   for a in (b.get("alerts") or [])[:6]],
        "largest_move": b.get("largest_move"),
        "alert_threshold": b.get("alert_threshold"),
        "source_tiers": {"counts_this_period": b.get("tier_counts"),
                         "legend": [{"tier": t.get("tier"), "label": t.get("label"), "weight": t.get("weight")}
                                    for t in (b.get("tier_legend") or [])],
                         "single_tier_notes": [_clip(x, 200) for x in (b.get("single_tier_notes") or [])[:6]],
                         "n_low_reliability_not_used": len(b.get("low_reliability_items") or [])},
        "tone_methods": b.get("method_counts"),
        "banners": [_clip(x) for x in b.get("banners") or []],
    }
    if with_items:
        facts["items"] = [{"title": _clip((i.get("item") or {}).get("title"), 160),
                           "source": (i.get("item") or {}).get("source_name"),
                           "source_tier": i.get("source_tier"),
                           "published": (i.get("item") or {}).get("published_at"),
                           "date_status": (i.get("item") or {}).get("date_status"),
                           "tickers": i.get("matched_tickers"),
                           "tone_by_fund": [{"ticker": t.get("ticker"), "label": t.get("label")}
                                            for t in (i.get("tone_by_fund") or i.get("fund_tones") or [])[:6]],
                           "summary": _clip(i.get("summary"), 240)}
                          for i in (b.get("items") or []) if i.get("source_tier") != "tier3"][:MAX_BRIEFING_ITEMS]
    return facts


# --------------------------------------------------------------------------
# Suggested questions (fixed rules, no LLM)
# --------------------------------------------------------------------------
def suggested_questions(profile: dict | None, portfolio: dict | None) -> list[str]:
    if portfolio is None:
        if profile is None:
            return ["How does EgoQuant build a portfolio?", "What is diversification?",
                    "What is the difference between risk tolerance and risk capacity?"]
        return [f"What does {profile['c_level']} mean for me?",
                "Which side limited my risk category, and why?",
                "What should I prepare before building my portfolio?"]
    out: list[str] = []
    if portfolio.get("approved_by_risk_engine") is False:
        out.append("Why did the Risk Engine not approve my portfolio?")
    holdings = sorted(portfolio.get("holdings") or [], key=lambda h: -(h.get("weight") or 0))
    if holdings:
        out.append(f"Why is {holdings[0]['ticker']} my largest holding?")
    goal = portfolio.get("goal")
    if goal:
        out.append("How likely am I to reach my goal, and what could I change to improve my chances?"
                   if goal.get("levers_what_would_change_the_odds")
                   else "How likely am I to reach my goal, and what drives that?")
    out.append("What happens to my portfolio in the worst stress scenario?")
    if (portfolio.get("class_weights") or {}).get("cash", 0) > 0.05:
        out.append("Why does my portfolio keep some cash?")
    if portfolio.get("what_changed"):
        out.append("What changed for my holdings recently?")
    elif portfolio.get("market_briefing"):
        out.append("Summarize this week's news for my holdings.")
    out.append("Which of my limits came from my risk profile?")
    return out[:4]
