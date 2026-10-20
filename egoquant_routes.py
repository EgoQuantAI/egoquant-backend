"""HTTP adapter for the EgoQuant engine (questionnaire, agents A/B/C, Risk Engine, report).

    GET  /questionnaire       question bank for the form (no points, no answer key)
    GET  /portfolio/options   allowed interest areas, trading platforms and game choices
    POST /profile             answers -> C-level and plain-language explanation
    POST /preferences/form    answers (+ trading platforms) -> the investment-type questions, each option
                              marked available or not for this risk profile (with the reason)
    POST /portfolio           answers + preference answers (or interest areas) + intake (+ optional risk
                              game) -> Agent C fetches data for those interests -> full pipeline -> report

Numbers come only from the deterministic engine; nothing here calls an LLM. Market data is
REAL (Agent C: Tiingo + Binance) unless EGOQUANT_DATA=synthetic, and every report says which.
The service keeps no state: the frontend sends the answers again with each request.
"""
import json
import platform
import logging
import os
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from threading import BoundedSemaphore, Lock, Thread
from typing import Literal, get_args

from fastapi import APIRouter, Depends, HTTPException
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator, ValidationError

from egoquant.advisor import build_portfolio_facts, seal_context
from egoquant.agents.behavioral import BehavioralTwinAgent
from egoquant.agents.behavioral.models import (BehavioralData, BehaviorConfig, CrashTestResult, GameResult,
                                               LossAcceptance)
from egoquant.agents.market_intel import (
    MarketDataUnavailable,
    PriceSourceError,
    SyntheticPriceSource,
)
from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, InterestArea
from fastapi import Header

from egoquant.accounts.ibkr_input import IbkrFlexIn, load_ibkr_report, rows_to_report
from egoquant.accounts.routes import _call, _own, _signed_in, has_consent, log_action
from egoquant.accounts.routes import router as account_router
from egoquant.accounts.store import get_store
from egoquant.agents.behavioral.ibkr import CASH_ID, IbkrHistory, IbkrReportError, to_trading_history
from egoquant.agents.behavioral.ibkr import MAX_BYTES as IBKR_MAX_BYTES
from egoquant.agents.market_intel.directory import Directory, load_directory, merge_catalog
from egoquant.agents.market_intel.sources import build_live_source
from egoquant.agents.market_intel.platforms import PLATFORMS, normalize_platform
from egoquant.demo.personas import PERSONAS as _DEMO_PERSONAS
from egoquant.demo.synthetic_investors import EG_PAYOFFS
from egoquant.agents.quant.goal import GoalSpec
from egoquant.briefing.web import build_web_briefing
from egoquant.models import AgentConfig
from egoquant.pipeline import run_pipeline
from egoquant.risk_engine import plain_violations
from egoquant import settings, trade_check
from egoquant.agents.market_intel.snapshot import agent_c_snapshot_path, load_snapshot
from egoquant.advisor.trade_explain import seal_trade_context
from egoquant.trade_check import PlannedTrade, TradeCheckError
from egoquant.preferences import PreferenceAnswers, build_form, load_preference_bank
from egoquant.preferences.form import to_interest_fields
from egoquant.report import render_html, what_changed_lines
from egoquant.risk_profile import Answers, GameInput, build_risk_profile, explain, load_all, to_agent_c_limits
from egoquant.risk_profile.loader import QUESTIONS_PATH

router = APIRouter()
logger = logging.getLogger(__name__)

BANK, PROFILE_CONFIG = load_all()
PREF_BANK = load_preference_bank()
with open(QUESTIONS_PATH, encoding="utf-8") as _fh:
    SECTIONS = [{"id": s["id"], "title": s["title"]} for s in json.load(_fh)["sections"]]
INVESTOR_ID = "web-investor"              # no accounts yet; the service is stateless
GAME_VERSION = "eg6-v1"

# The full pipeline is expensive (market data + optimization), so it gets its own limit.
_PORTFOLIO_LIMIT = 20
_PORTFOLIO_WINDOW = 60.0
_portfolio_timestamps: deque = deque()
_portfolio_lock = Lock()
# At most this many pipeline runs at once (settings.max_concurrent_builds, default 1): the others wait their turn, up to build_wait_seconds,
# then get a 503 with Retry-After. Taken just before the pipeline runs (after validation, scoring and IBKR input) and always given back.
_build_slots = BoundedSemaphore(settings.max_concurrent_builds())
# Agent C's directory of every US stock and ETF takes minutes to build (downloads + parsing), and Railway's disk
# cache is wiped on each redeploy. So it is loaded in a background thread, never inside a request; until it is
# ready, live requests use the curated catalog (what Agent C does offline anyway).
_DIRECTORY_RETRY_SECONDS = 600.0       # after a failed load, try again at most this often
_directory: Directory | None = None
_directory_checked_on: date | None = None
_directory_attempt_at: float | None = None
_directory_loading = False
_directory_lock = Lock()
_DEBUG_LIMIT = 5                       # /debug/portfolio has its own, smaller bucket: it can never use up users' requests
_debug_timestamps: deque = deque()


# --------------------------------------------------------------------------
# Request and response bodies
# --------------------------------------------------------------------------
class AnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questionnaire_version: str | None = None          # if sent, must match the server's bank
    choices: dict[str, str] = Field(max_length=60)
    multi_choices: dict[str, list[str]] = Field(default_factory=dict, max_length=10)
    amount_invested: float = Field(gt=0, le=1e12)
    amount_needed: float = Field(ge=0, le=1e12)
    # The app's Risk Game result (docs/RISK_GAME_BLEND.md): one third of the tolerance part of the risk category. It rides with
    # the answers so /profile, /preferences/*, /portfolio and /trade-check all score the same category. None = not played.
    risk_game: "RiskGameIn | None" = None

    def game_input(self) -> GameInput | None:
        return self.risk_game.game_input() if self.risk_game is not None else None

    @model_serializer(mode="wrap")
    def _leave_out_no_game(self, handler):
        """Answers without a game dump exactly as before the game existed (saved answers, sealed what-if / chat inputs)."""
        data = handler(self)
        if data.get("risk_game") is None:
            data.pop("risk_game", None)
        return data

    def to_answers(self) -> Answers:
        if self.questionnaire_version not in (None, BANK.questionnaire_version):
            raise HTTPException(409, f"The questionnaire changed (now {BANK.questionnaire_version}). "
                                     "Please reload it and answer again.")
        try:
            return Answers(questionnaire_version=BANK.questionnaire_version, choices=self.choices,
                           multi_choices=self.multi_choices, amount_invested=self.amount_invested,
                           amount_needed=self.amount_needed)
        except ValidationError as error:
            raise HTTPException(422, _first_error(error)) from None


class GameIn(BaseModel):
    """Eckel-Grossman risk game: one of six 50/50 gambles (1 = sure thing ... 6 = riskiest)."""
    model_config = ConfigDict(extra="forbid")
    choice: int = Field(ge=1, le=6)
    played_at: datetime

    @field_validator("played_at")
    @classmethod
    def not_in_future(cls, value: datetime) -> datetime:
        return _not_in_future(value)

    def to_game_result(self) -> GameResult:
        low, high = EG_PAYOFFS[self.choice]
        return GameResult(choice=self.choice, payoff_low=low, payoff_high=high, version=GAME_VERSION,
                          timestamp=self.played_at.isoformat())


# PROVISIONAL: the app's Risk Game scores risk tolerance 0-100; Agent B reads an Eckel-Grossman choice
# 1-6. Equal-width bands map one onto the other (upper bound of each band -> choice). Not validated
# research; Agent B only ever tightens limits and never makes a game-based limit hard.
RISK_GAME_BANDS = [(16, 1), (33, 2), (50, 3), (66, 4), (83, 5), (100, 6)]


def risk_game_choice(tolerance: int) -> int:
    return next(choice for upper, choice in RISK_GAME_BANDS if tolerance <= upper)


class CrashTestIn(BaseModel):
    """The Risk Game's Crash Test: a balanced fund held through a fixed, hypothetical fall. Fractions 0-1."""
    model_config = ConfigDict(extra="forbid")
    sold_at_drawdown: float | None = Field(None, ge=0, le=1)   # loss from the peak at the first sale; None = held on
    sold_fraction: float = Field(0.0, ge=0, le=1)
    max_drawdown_seen: float = Field(ge=0, le=1)


class RiskGameIn(BaseModel):
    """Result of the app's Risk Game (src/app/game): scores 0-100, higher = more risk taken."""
    model_config = ConfigDict(extra="forbid")
    tolerance: int = Field(ge=0, le=100)
    loss_acceptance: int | None = Field(None, ge=0, le=100)
    crash_test: CrashTestIn | None = None
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def not_in_future(cls, value: datetime) -> datetime:
        value = _not_in_future(value)
        if value.year < 2024:                    # an old result is fine (the browser keeps it); a nonsense date is not
            raise ValueError("the game time is before 2024")
        return value

    def to_game(self) -> GameIn:
        return GameIn(choice=risk_game_choice(self.tolerance), played_at=self.completed_at)

    def game_input(self) -> GameInput:
        """What the risk profile blends into the category: the two 0-100 scores (the crash test is Agent B's alone)."""
        return GameInput(tolerance=self.tolerance, loss_acceptance=self.loss_acceptance)


AnswersIn.model_rebuild()


class PreferenceAnswersIn(BaseModel):
    """Answers to the investment-preference questions (option ids per question id)."""
    model_config = ConfigDict(extra="forbid")
    preferences_version: str
    choices: dict[str, list[str]] = Field(max_length=20)

    @field_validator("choices")
    @classmethod
    def bounded(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(len(ids) > 20 for ids in value.values()):
            raise ValueError("at most 20 options per question")
        return value

    def to_answers(self) -> PreferenceAnswers:
        if self.preferences_version != PREF_BANK.preferences_version:
            raise HTTPException(409, f"The investment questions changed (now {PREF_BANK.preferences_version}). "
                                     "Please answer them again.")
        return PreferenceAnswers(preferences_version=self.preferences_version, choices=self.choices)


class PreferenceFormIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: AnswersIn
    trading_platforms: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("trading_platforms")
    @classmethod
    def known_platforms(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_platform(p) for p in value))


class PortfolioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: AnswersIn
    # What Agent C fetches data for: the preference answers (the app's flow), or interest areas directly.
    preferences: PreferenceAnswersIn | None = None
    interest_areas: list[InterestArea] | None = Field(None, min_length=1, max_length=20)
    trading_platforms: list[str] = Field(default_factory=list, max_length=10)
    horizon_years: float = Field(gt=0, le=60)
    liquidity_need: float = Field(0.0, ge=0, le=1)
    min_cash_buffer: float = Field(0.0, ge=0, le=1)
    current_holdings: dict[str, float] | None = Field(None, max_length=50)
    # The investor's IBKR trading history (optional): an uploaded Flex trades report, or a Flex token to fetch it.
    ibkr_report: str | None = Field(None, max_length=IBKR_MAX_BYTES)
    ibkr_filename: str | None = Field(None, max_length=255)    # the upload's name (.xml / .csv), optional
    ibkr_flex: IbkrFlexIn | None = None
    game: GameIn | None = None                  # an Eckel-Grossman choice made directly
    risk_game: RiskGameIn | None = None         # or the app's Risk Game result (mapped to a choice)
    # Goal (optional). With a goal amount the goal-based path runs: many candidates, Agent B's shortlist,
    # a simulation of each against the goal, and the portfolio most likely to reach it.
    goal_amount: float | None = Field(None, gt=0, le=1e12)
    monthly_flow: float = Field(0.0, ge=-1e9, le=1e9)    # + monthly contribution, - monthly withdrawal

    @model_validator(mode="after")
    def one_game(self) -> "PortfolioIn":
        if self.game is not None and (self.risk_game is not None or self.answers.risk_game is not None):
            raise ValueError("send either game or risk_game, not both")
        if self.risk_game is not None and self.answers.risk_game is not None and self.risk_game != self.answers.risk_game:
            raise ValueError("send the Risk Game result once (inside answers)")
        if (self.preferences is None) == (self.interest_areas is None):
            raise ValueError("send either preferences or interest_areas (exactly one)")
        if self.ibkr_report is not None and self.ibkr_flex is not None:
            raise ValueError("send either ibkr_report or ibkr_flex, not both")
        return self

    @field_validator("trading_platforms")
    @classmethod
    def known_platforms(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_platform(p) for p in value))   # ValueError lists the allowed ones


class ProfileOut(BaseModel):
    questionnaire_version: str
    c_level: str
    explanation: list[str]
    tolerance_score: int               # Grable & Lytton total, 13-47
    tolerance_level: int
    capacity_level: int
    loss_capacity: float
    knowledge_score: int               # Big Three correct, 0-3
    binding_dimension: str             # "tolerance", "capacity" or "gate"
    no_loss_gate: bool
    suitability_max_equity: float
    product_access: str
    max_r_level: int
    blocked_product_types: list[str]
    flags: list[str]
    # Only when a Risk Game result was sent (docs/RISK_GAME_BLEND.md); c_level and everything above are the blended ones.
    game_played: bool | None = None
    game_score: float | None = None
    game_level: int | None = None
    blended_level: int | None = None
    questionnaire_class: str | None = None             # the category WITHOUT the game


class PortfolioOut(BaseModel):
    approved: bool                     # Risk Engine verdict: only an approved portfolio may be offered
    status: str                        # Agent A: optimal / fallback / infeasible
    c_level: str
    as_of: date
    data_source: str                   # "historical" (real prices) or "synthetic"
    violations: list[str]
    checks: list[dict]
    game_note: str | None              # how the risk game entered the calculation, if it did
    goal: dict | None = None           # goal path only: Agent B's shortlist and the simulation (see _goal_summary)
    universe: dict | None = None       # what Agent C priced for this investor (see _universe_summary)
    goal_unavailable: str | None = None  # a goal was given but not simulated (too little price history): why
    fallback_reason: str | None = None  # why the live feed was replaced (by the saved snapshot or SYNTHETIC prices): safe label
    prices_as_of: date | None = None    # set when the prices came from the committed snapshot: the day they run to (as_of is the request's)
    price_note: str | None = None       # e.g. "real prices as of 2026-10-16, cached (Yahoo Finance; crypto from Binance)"
    snapshot_first: bool = False        # the saved snapshot was CHOSEN (EGOQUANT_PRICE_MODE=snapshot), not a fallback
    trading_history: dict | None = None  # IBKR history Agent B used: counts, dates, notes (never raw trades)
    report_id: str | None = None         # /account/portfolio only: the saved report's id
    report: dict                       # ClientReport as JSON (numbers, reasons, banners)
    html: str                          # the same report as one self-contained, escaped HTML page
    chat_context: str | None = None    # sealed facts for the advisor chat (egoquant/advisor); send back to /chat
    briefing_context: str | None = None   # sealed inputs for POST /briefing/start (Agent D's full news briefing)
    what_if_context: str | None = None    # sealed inputs for the What-If Agent (chat tool, POST /what-if); never shown
                                          # to the LLM (egoquant/agents/what_if.py)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.get("/questionnaire")
def questionnaire():
    """Question texts and option texts only: points and the answer key stay on the server."""
    return {
        "questionnaire_version": BANK.questionnaire_version,
        "sections": SECTIONS,
        "questions": [
            {"id": q.id, "section": q.section, "type": q.type, "text": q.text,
             **({"hint": q.hint} if q.hint else {}),
             "options": [{"id": o.id, "text": o.text} for o in q.options]}
            for q in BANK.questions
        ],
    }


@router.get("/portfolio/options")
def portfolio_options():
    return {
        "interest_areas": list(get_args(InterestArea)),
        "trading_platforms": [{"id": key, "name": p.name} for key, p in PLATFORMS.items()],
        "game_choices": [{"choice": c, "low": low, "high": high} for c, (low, high) in EG_PAYOFFS.items()],
        # Products behind each interest, so the form can show which interests a profile may buy
        "interest_products": {
            area: [{"ticker": i.ticker, "r_level": i.r_level, "product_type": i.product_type}
                   for i in DEFAULT_CATALOG if area in i.areas]
            for area in get_args(InterestArea)
        },
        "data": _data_mode(),
    }


@router.post("/profile", response_model=ProfileOut, response_model_exclude_none=True)
def profile(body: AnswersIn):
    answers = body.to_answers()
    risk = _score(answers, body.game_input())
    return ProfileOut(
        questionnaire_version=risk.questionnaire_version, c_level=risk.c_level,
        explanation=explain(risk, answers, BANK, PROFILE_CONFIG),
        tolerance_score=risk.tolerance_score, tolerance_level=risk.tolerance_level,
        capacity_level=risk.capacity_level, loss_capacity=risk.loss_capacity,
        knowledge_score=risk.knowledge_score, binding_dimension=risk.binding_dimension,
        no_loss_gate=risk.no_loss_gate, suitability_max_equity=risk.suitability_max_equity,
        product_access=risk.product_access, max_r_level=risk.max_r_level,
        blocked_product_types=to_agent_c_limits(risk, PROFILE_CONFIG)["blocked_product_types"], flags=risk.flags,
        **_game_fields(risk),
    )


def _library_versions() -> dict:
    """The versions of the numerical libraries this server runs (no secrets): a crash or a slow build can then be tied to a stack."""
    from importlib import metadata
    out = {}
    for name in ("pandas", "numpy", "scipy"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


@router.get("/data/status")
def data_status():
    """Agent C's background price-cache builder: state, decision date, tickers planned and warm."""
    from egoquant.agents.market_intel.sources import tiingo_requests_in_last
    from egoquant.agents.market_intel.warm import warmer_status
    from egoquant import settings
    from egoquant.briefing.worker import news_worker_status
    return {**warmer_status(), "news_worker": news_worker_status(),
            # How this server gets prices and builds portfolios (no secrets): visible so a deploy can be checked from outside.
            "price_mode": settings.price_mode(), "live_budget_seconds": settings.live_budget_seconds(),
            "slow_request_seconds": settings.slow_request_seconds(), "portfolio_method": getattr(settings, "portfolio_method", lambda: "v1")(),
            "python_version": platform.python_version(), "libraries": _library_versions(),
            # The Risk Game's role in the category as THIS server applies it (docs/RISK_GAME_BLEND.md): the share of the tolerance part
            # and whether it may raise it; the environment variable if set, else the risk profile's own default.
            "risk_game_weight": (settings.risk_game_weight() if settings.risk_game_weight() is not None
                                 else PROFILE_CONFIG.game.weight),
            "risk_game_may_raise": (False if settings.risk_game_may_raise() is False else PROFILE_CONFIG.game.may_raise),
            "traders_key_requests_last_hour": tiingo_requests_in_last(3600, settings.tiingo_api_key() or None),
            "builder_key_requests_last_hour": tiingo_requests_in_last(3600, settings.tiingo_warm_api_key() or None)}


class BriefingStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    briefing_context: str = Field(min_length=16, max_length=120_000)


_briefing_timestamps: deque = deque()


@router.post("/briefing/start")
def briefing_start(body: BriefingStartIn):
    """Start Agent D's full news briefing for a portfolio built by /portfolio (its sealed briefing_context). It
    rates every relevant headline (holdings first) outside any request; poll GET /briefing/{job}."""
    from egoquant.briefing.jobs import BriefingContextError, open_briefing_context, start_job
    from egoquant.briefing.loader import load_config as load_briefing_config
    _check_portfolio_rate(_briefing_timestamps, load_briefing_config().briefing_job.per_window)
    try:
        facts = open_briefing_context(body.briefing_context)
    except BriefingContextError:
        raise HTTPException(400, "This portfolio's news context is invalid or expired. Please rebuild the portfolio.") from None
    return start_job(facts).public()


@router.get("/briefing/{job_id}")
def briefing_status(job_id: str):
    """Progress {rated, total}; when done, the week and month briefings and "What changed" for both."""
    from egoquant.briefing.jobs import get_job
    job = get_job(job_id) if len(job_id) <= 64 else None
    if job is None:
        raise HTTPException(404, "Unknown or expired news briefing. Please open the News tab again.")
    return job.public()


class FinalReportIn(BaseModel):
    """The browser's copy of the report (its Agent D sections may be left out: they are replaced) and the Advisor's
    sealed chat context, to be refreshed with the job's final briefing."""
    model_config = ConfigDict(extra="forbid")
    report: dict
    chat_context: str | None = Field(None, max_length=120_000)


class SavedFinalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _final_error(err) -> HTTPException:
    return HTTPException(err.status, str(err))


@router.post("/briefing/{job_id}/report")
def briefing_final_report(job_id: str, body: FinalReportIn):
    """One source of truth: the report with the briefing JOB's final Agent D sections (the News tab's briefing,
    period, ratings and "What changed"), its HTML page and the refreshed Advisor context. Agent A's and the Risk
    Engine's numbers are checked against the digest sealed at /portfolio time and never changed. 409 while the
    news is still being rated (the page waits and asks again)."""
    from egoquant.briefing import final
    from egoquant.briefing.jobs import get_job
    job = get_job(job_id) if len(job_id) <= 64 else None
    try:
        result = final.check_job(job, body.report)
    except final.FinalBriefingError as err:
        raise _final_error(err) from None
    report = final.apply_final(body.report, final.final_sections(result))
    try:
        html = final.render_report_html(report)
    except ValidationError:
        raise HTTPException(422, "This report could not be read.") from None
    return {"report": report, "html": html, "chat_context": final.refresh_chat_context(body.chat_context, report),
            "job": job_id}


@router.post("/account/reports/{report_id}/briefing")
def saved_report_final_briefing(report_id: str, body: SavedFinalIn, authorization: str | None = Header(None)):
    """The signed-in user's saved report takes the job's final Agent D sections (same check as above: the job must
    belong to this report). Only those sections are written, through replace_report_briefing (migration 004)."""
    from uuid import UUID
    from egoquant.briefing import final
    from egoquant.briefing.jobs import get_job
    try:
        UUID(report_id)
    except ValueError:
        raise HTTPException(404, "Report not found.") from None
    token, user = _signed_in(authorization)
    rows = _call(get_store().select, token, "reports", _own(user, id=f"eq.{report_id}", select="report", limit="1"))
    if not rows:
        raise HTTPException(404, "Report not found.")
    saved = rows[0].get("report") or {}
    from egoquant.accounts.sealed import require_unsealed
    try:                                       # a sealed report no longer changes (plain 409); no seal tables yet = not sealed
        seal_rows = _call(get_store().select, token, "advisory_records",
                          _own(user, report_id=f"eq.{report_id}", select="id", limit="1"))
    except HTTPException:
        seal_rows = []
    require_unsealed(seal_rows)
    try:
        result = final.check_job(get_job(body.job), saved.get("report") or {})
    except final.FinalBriefingError as err:
        raise _final_error(err) from None
    sections = final.final_sections(result)
    # The same size rule as when the report was saved. A large finished briefing (hundreds of headlines) keeps its
    # WEEK view, the default one, and drops the month view first; only a week too large on its own is dropped too.
    # (The download still shows the full finished news: it is rendered from the job, anchoring/routes.py.)
    if len(json.dumps(sections)) > _MAX_SAVED_REPORT_BYTES:
        sections["market_briefing_month"] = None
    if len(json.dumps(sections)) > _MAX_SAVED_REPORT_BYTES:
        sections["market_briefing"] = None
    _call(get_store().rpc, token, "replace_report_briefing", {"rid": report_id, "sections": sections})
    return {"ok": True, "report_id": report_id}


@router.post("/preferences/form")
def preferences_form(body: PreferenceFormIn):
    """The investment-type questions for THIS profile. Unavailable options carry the reason."""
    risk = _score(body.answers.to_answers(), body.answers.game_input())
    return build_form(PREF_BANK, risk, PROFILE_CONFIG, body.trading_platforms).model_dump()


class PreferenceAreasIn(PreferenceFormIn):
    preferences: PreferenceAnswersIn


_PREFERENCE_ECHOES = ("answers to unknown questions", "unknown option")      # these two sentences repeat ids the client sent


def _preference_error(error: ValueError) -> str:
    """The plain sentence of a preference-answer error ("You chose Technology both as an industry you are interested in and
    as one to avoid ..."), except for the two that would repeat what the client sent: those get one generic sentence."""
    text = str(error)
    if any(part in text for part in _PREFERENCE_ECHOES):
        return "One of the chosen options is not on the investment form. Please reload the questions and choose again."
    return text


@router.post("/preferences/areas")
def preferences_areas(body: PreferenceAreasIn):
    """The interest areas a portfolio request with these preference answers would use, in the order the build takes them, and the
    number of areas it will actually use. The investment page shows this BEFORE the build, so nobody is surprised by what is left out."""
    risk = _score(body.answers.to_answers(), body.answers.game_input())
    form = build_form(PREF_BANK, risk, PROFILE_CONFIG, body.trading_platforms)
    try:
        fields = to_interest_fields(body.preferences.to_answers(), form, PREF_BANK)
    except ValueError as error:
        raise HTTPException(422, _preference_error(error)) from None
    return {"areas": fields["interest_areas"], "limit": PORTFOLIO_MAX_INTEREST_AREAS}


class IbkrCheckIn(BaseModel):
    """The IBKR input of the Investments step, checked before the build: exactly one of a report or a Flex token."""
    model_config = ConfigDict(extra="forbid")
    ibkr_report: str | None = Field(None, max_length=IBKR_MAX_BYTES)
    ibkr_filename: str | None = Field(None, max_length=255)
    ibkr_flex: IbkrFlexIn | None = None


_IBKR_CHECK_LIMIT = 30                 # per minute, all users (several judges at once); each Flex check is one request to IBKR's server
_ibkr_check_timestamps: deque = deque()


@router.post("/portfolio/ibkr-check")
def ibkr_check(body: IbkrCheckIn):
    """Read the IBKR report (or fetch it once with the Flex token) and say what is in it, so a bad file or an expired
    token is caught on the Investments step and not minutes later at the build. Nothing is stored; only counts and
    dates come back, never a trade, the account id or the token. Errors are the same user-safe 422s as /portfolio."""
    if (body.ibkr_report is None) == (body.ibkr_flex is None):     # checked here, not in the model: a model error
        raise HTTPException(422, "Send either an IBKR Flex report or a Flex token.")   # would echo the input
    _check_portfolio_rate(_ibkr_check_timestamps, _IBKR_CHECK_LIMIT)
    report, source = load_ibkr_report(body.ibkr_report, body.ibkr_flex, body.ibkr_filename)
    return {"source": source, "n_trades": len(report.trades), "n_cash_flows": len(report.cash_transactions),
            "start": report.start.isoformat(), "end": report.end.isoformat(), "currency": report.currency,
            "notes": report.notes}


@router.post("/portfolio", response_model=PortfolioOut)
def portfolio(body: PortfolioIn):
    return _build_portfolio(body)


_MAX_SAVED_REPORT_BYTES = 1_500_000     # the reports table accepts < 2 MB per row


@router.post("/account/portfolio", response_model=PortfolioOut)
def account_portfolio(body: PortfolioIn, authorization: str | None = Header(None)):
    """Signed-in users: the portfolio built from the trading history saved in their account, then saved as one
    of their reports. The IBKR data is connected on the account page (/account/trades/import), not sent here."""
    token, user = _signed_in(authorization)
    if body.ibkr_report is not None or body.ibkr_flex is not None:
        raise HTTPException(422, "Connect IBKR data on your account page; this request uses the saved history.")
    store = get_store()
    trades = _call(store.select, token, "trades", _own(user, order="trade_date.asc"))
    flows = _call(store.select, token, "cash_flows", _own(user, order="day.asc")) if trades else []
    out = _build_portfolio(body, stored=(rows_to_report(trades, flows), "account") if trades else None,
                           user_id=user.id)
    saved = out.model_dump(mode="json", exclude={"html", "chat_context", "report_id", "what_if_context"})
    if len(json.dumps(saved)) > _MAX_SAVED_REPORT_BYTES:            # keep the numbers, drop the long news lists
        for key in ("market_briefing", "market_briefing_month"):
            saved["report"][key] = None
    # The answers the report was built from are kept with it (the owner's own row, 180 days, erased with the rest):
    # on-chain sealing (egoquant/anchoring) fingerprints them as `inputs.answers`. No questionnaire score is stored.
    answers_saved = body.answers.model_dump(mode="json", exclude=None if body.answers.risk_game is not None else {"risk_game"})
    answers_saved["questionnaire_version"] = answers_saved["questionnaire_version"] or BANK.questionnaire_version
    saved["inputs"] = {"answers": answers_saved}
    # The What-If Agent's base facts, exactly as this build sealed them (minus report_id, added once the row has an id),
    # so a saved report can be simulated later: /account/reports/{id}/contexts re-seals them as of this build. They
    # need the build's own run (Agent A's menu, the sampler record, the market snapshot), which nothing else keeps.
    # Too large for the row: None, which the contexts endpoint reports as "not kept" (an older row has no key at all).
    if out.what_if_context:
        from egoquant.agents.what_if import WhatIfInvalid, open_what_if_context
        try:
            saved["what_if_facts"] = open_what_if_context(out.what_if_context)
        except WhatIfInvalid:
            saved["what_if_facts"] = None
        if saved["what_if_facts"] is not None and len(json.dumps(saved)) > _MAX_SAVED_REPORT_BYTES:
            logger.warning("what-if facts left out of a saved report (row too large)")
            saved["what_if_facts"] = None
    summary = {"approved": out.approved, "as_of": out.as_of.isoformat(), "data_source": out.data_source,
               "equity_weight": out.report.get("equity_weight"), "n_trades": (out.trading_history or {}).get("n_trades", 0),
               "prob_goal": (out.goal or {}).get("chosen", {}).get("prob_goal")}
    if out.prices_as_of is not None:                       # snapshot builds: the day the saved prices run to
        summary["prices_as_of"] = out.prices_as_of.isoformat()
    row = _call(store.insert, token, "reports", [{"user_id": user.id, "summary": summary, "report": saved}])
    out.report_id = str(row[0]["id"]) if row else None
    if out.what_if_context and out.report_id:          # what-if runs on this portfolio name the saved report
        from egoquant.agents.what_if import open_what_if_context, seal_what_if_context
        out.what_if_context = seal_what_if_context({**open_what_if_context(out.what_if_context),
                                                    "report_id": out.report_id})
    try:                                    # a new action name: the database accepts it only after migration 006
        log_action(token, user, "questionnaire_submitted",
                   {"report_id": out.report_id, "questionnaire_version": BANK.questionnaire_version})
    except HTTPException:
        logger.warning("questionnaire_submitted not logged (is migration 006 applied?)")
    log_action(token, user, "report_generated", {"report_id": out.report_id, "approved": out.approved})
    if not out.approved or out.violations:
        log_action(token, user, "warning_shown", {"report_id": out.report_id, "kind": "not_approved"})
    return out


class TradeCheckIn(PortfolioIn):
    """The same facts as /portfolio (they rebuild the investor's profile and limits) plus the planned trade.
    The holdings come from the account's saved trades, never from the request."""
    planned: PlannedTrade


@router.post("/account/trade-check")
def account_trade_check(body: TradeCheckIn, authorization: str | None = Header(None)):
    """Check ONE planned trade against the investor's limits (deterministic; egoquant/trade_check.py).
    The verdict and its numbers are saved with the user's checks; nothing is traded."""
    token, user = _signed_in(authorization)
    if body.ibkr_report is not None or body.ibkr_flex is not None or body.current_holdings is not None:
        raise HTTPException(422, "The trade check uses the trading history saved in your account.")
    if not has_consent(token, user, "trade_check"):
        raise HTTPException(403, "Please agree to the trade-check terms first.")
    _check_portfolio_rate()
    store = get_store()
    trades = _call(store.select, token, "trades", _own(user, order="trade_date.asc"))
    flows = _call(store.select, token, "cash_flows", _own(user, order="day.asc")) if trades else []
    stored = (rows_to_report(trades, flows), "account")                 # 404 when nothing is connected yet
    check = _trade_check_for(body, stored)
    result = check.model_dump(mode="json")
    row = _call(store.insert, token, "trade_checks", [{"user_id": user.id, "planned": result["planned"],
                                                       "verdict": check.verdict, "result": result}])
    check_id = str(row[0]["id"]) if row else None
    log_action(token, user, "trade_checked", {"check_id": check_id, "verdict": check.verdict,
                                              "ticker": body.planned.ticker, "side": body.planned.side})
    if check.verdict != "advisable":
        log_action(token, user, "warning_shown", {"check_id": check_id, "kind": check.verdict})
    return {"check_id": check_id, "check": result, "trade_context": _trade_context(check)}


@router.post("/trade-check")
def visitor_trade_check(body: TradeCheckIn):
    """The same check for visitors without an account: the holdings come from the IBKR report or Flex token
    sent with THIS request (used once, nothing stored). No verdict is saved and no trade can be recorded."""
    if (body.ibkr_report is None) == (body.ibkr_flex is None) or body.current_holdings is not None:
        raise HTTPException(422, "Send your IBKR trading history (a Flex report or a Flex token) so the check "
                                 "knows what you hold.")
    _check_portfolio_rate()
    check = _trade_check_for(body, None)
    return {"check_id": None, "saved": False, "check": check.model_dump(mode="json"),
            "trade_context": _trade_context(check)}


def _trade_context(check) -> str | None:
    """The check's facts sealed for the advisor chat (it explains only a check it can verify). Optional:
    without it the verdict panel still works, there is just no plain-words explanation."""
    try:
        return seal_trade_context(check)
    except Exception:
        logger.exception("Trade check could not be sealed for the chat")
        return None


def _trade_check_for(body: TradeCheckIn, stored, resolve_planned=None) -> "trade_check.TradeCheck":
    """Rebuild the investor's context (profile, limits, Agent B) and check the planned trade against the holdings
    the IBKR history ends with (amounts; account cash is the holding CASH). 422 when it cannot be checked.

    resolve_planned(planned, holdings) -> planned: the advisor chat sizes "sell 30% of VTI" from the holdings found here (it may
    raise TradeCheckError, shown as the 422 sentence); the Trade check page sends an amount and passes nothing."""
    _, as_of, run, _, ibkr, _ = _execute(body.model_copy(update={"goal_amount": None, "monthly_flow": 0.0}), stored)
    holdings = {t: w * ibkr.final_value for t, w in ibkr.final_weights.items() if w * ibkr.final_value > 1e-9}
    assets = run.agent_c.get_market_snapshot(run.selection.tickers, as_of, AgentConfig().lookback_days).assets
    try:
        planned = resolve_planned(body.planned, holdings) if resolve_planned is not None else body.planned
        check = trade_check.check_trade(
            holdings=holdings, planned=planned, profile=run.profile, profile_config=PROFILE_CONFIG,
            selection=run.selection, assets=assets, behavioral_constraints=run.result.behavioral_constraints,
            as_of=as_of, min_cash_buffer=body.min_cash_buffer, liquidity_need=body.liquidity_need)
    except TradeCheckError as error:
        raise HTTPException(422, str(error)) from None
    note = _price_source_note(run)
    if note:
        check.notes.append(note)
    return check


def _price_source_note(run) -> str | None:
    """Where the product list and prices behind a check came from, when that was NOT the live feed (else None)."""
    market = getattr(run, "market_report", None)
    if market is None or not getattr(market, "price_note", None) or getattr(market, "prices_as_of", None) is None:
        return None
    if getattr(market, "snapshot_first", False):
        return (f"The product list for this check was built on the {market.price_note}. "
                f"Markets after {market.prices_as_of.isoformat()} are not in it.")
    return (f"Live prices were unavailable, so the product list for this check was built on saved {market.price_note}. "
            f"Markets after {market.prices_as_of.isoformat()} are not in it.")


def _build_portfolio(body: PortfolioIn, stored=None, user_id: str | None = None) -> PortfolioOut:
    """stored: (IbkrReport, source) from a signed-in user's saved trades, or None (the request's own IBKR data).
    user_id: the signed-in user (/account/portfolio); the sealed what-if inputs are then bound to them."""
    _check_portfolio_rate()
    risk, as_of, run, game_note, ibkr, ibkr_summary = _execute(body, stored)
    out = _portfolio_out(risk, as_of, run, game_note, ibkr_summary)
    out.what_if_context = _what_if_context(body, stored, run, as_of, user_id)
    return out


def _what_if_context(body: PortfolioIn, stored, run, as_of: date, user_id: str | None) -> str | None:
    """Sealed inputs for the What-If Agent: the request WITHOUT any raw IBKR report or Flex token, the results it
    needs and the hard limits. Never shown to the LLM. Never fails the portfolio."""
    try:
        from egoquant.agents.what_if import build_what_if_facts, seal_what_if_context
        source = "account" if stored is not None else ("ibkr_flex" if body.ibkr_flex is not None
                                                      else "ibkr_report" if body.ibkr_report is not None else None)
        inputs = body.model_dump(mode="json", exclude={"ibkr_report", "ibkr_flex", "ibkr_filename"})
        assets = run.agent_c.get_market_snapshot(run.selection.tickers, as_of, AgentConfig().lookback_days).assets
        return seal_what_if_context(build_what_if_facts(body=inputs, run=run, as_of=as_of, user_id=user_id,
                                                        ibkr_source=source, assets=assets))
    except Exception:                                   # the what-if is optional; the portfolio is not
        logger.exception("what-if context could not be built; the chat will say it cannot simulate")
        return None


# /portfolio time budget. PROVISIONAL. Measured 19 Oct (20 interest areas, empty cache): the in-request news briefing
# took up to 62 s locally (AI summaries) and the deployed build answered 502 after 249 s. Agent A and the Risk Engine
# took under 0.5 s and are never limited or skipped; only the optional parts below are.
PORTFOLIO_PRICE_FETCH_SECONDS = 6.0     # Agent C: no NEW price request for an optional fund after this (core funds and
                                        # holdings are always priced; cached prices are always used). 10 s before 20 Oct:
                                        # a cold 20-area build took 10.6 s, ~9 s of it here; every fetched price is cached,
                                        # so the next builds include the funds left out this time.
PORTFOLIO_BRIEFING_SECONDS = 5.0        # Agent D's in-request briefing (cache only): hard limit, then sent without it
PORTFOLIO_MAX_INTEREST_AREAS = 8        # interest areas used for prices and news: the investor's first ones


def _budgeted_briefing(**kwargs):
    """The in-request news briefing: cache only (no feed request, no OpenAI call) and at most
    PORTFOLIO_BRIEFING_SECONDS. Late or failed = None: the report is sent without it and the briefing job
    (/briefing/start, with its progress bar) fills the News tab. Never raises."""
    box: dict = {}

    def run() -> None:
        box["briefing"] = build_web_briefing(cache_only=True, **kwargs)

    worker = Thread(target=run, name="portfolio-briefing", daemon=True)
    worker.start()
    worker.join(PORTFOLIO_BRIEFING_SECONDS)
    if worker.is_alive():
        logger.warning("In-request briefing not ready within %.0f s; report sent without it (the briefing job "
                       "continues)", PORTFOLIO_BRIEFING_SECONDS)
        return None
    return box.get("briefing")


def _execute(body: PortfolioIn, stored=None, *, briefing: bool = True, agent_config: AgentConfig | None = None,
             extra_excluded: list[str] | None = None, goal_start: float | None = None, as_of: date | None = None):
    """Everything up to and including the pipeline run: (risk, as_of, run, game_note, ibkr, ibkr_summary).

    The keyword arguments are for the What-If Agent's reoptimize mode only (defaults = the /portfolio behaviour):
    briefing=False skips the news briefing, agent_config sets fewer goal paths, extra_excluded removes assets
    (tighten-only), goal_start replaces the goal's start amount WITHOUT changing the questionnaire answers (so
    the risk class and limits stay the same), and as_of keeps the base portfolio's decision date."""
    answers = body.answers.to_answers()
    risk_game = body.answers.risk_game or body.risk_game      # the same result everywhere (PortfolioIn.one_game)
    game_input = risk_game.game_input() if risk_game is not None else None
    risk = _score(answers, game_input)                      # clear 422 before any market data is fetched
    preferences = body.preferences.to_answers() if body.preferences is not None else None
    synthetic = _data_mode() == "synthetic"
    directory = "auto" if synthetic else _directory_for_request()   # synthetic runs never use the directory
    as_of = as_of or _last_weekday_before(date.today())
    behavioral, game_note, loss_acceptance, crash_test = None, None, None, None
    game = body.game
    in_profile = bool(getattr(risk, "game_played", False))     # the game already sits inside the category (one third)
    if risk_game is not None:
        game = risk_game.to_game()
        if in_profile:
            game_note = (f"Risk Game played: score {risk.game_score:.0f}/100, level {risk.game_level} of 5. It counts one third of "
                         f"your attitude to risk (the questionnaire two thirds); your finances then set a cap. Your category: {risk.c_level}.")
        else:
            game_note = (f"Risk Game tolerance {risk_game.tolerance}/100 was read as gamble {game.choice} of 6 "
                         "(the 0-100 score is split into six equal bands). It can only make the portfolio more careful.")
        if risk_game.loss_acceptance is not None:
            loss_acceptance = LossAcceptance(score=risk_game.loss_acceptance,
                                             timestamp=risk_game.completed_at.isoformat())
            if not in_profile:
                game_note += (f" Loss acceptance {risk_game.loss_acceptance}/100 (safer or riskier bets after "
                              "losing rounds) is read the same way: it can only make the portfolio more careful.")
        if risk_game.crash_test is not None:
            try:
                crash_test = CrashTestResult(**risk_game.crash_test.model_dump(),
                                             timestamp=risk_game.completed_at.isoformat())
            except ValidationError as error:
                raise HTTPException(422, "crash_test: " + _first_error(error)) from None
            ct = crash_test
            game_note += (f" Crash test: you sold {ct.sold_fraction:.0%} of the fund when it was down "
                          f"{ct.sold_at_drawdown:.0%}." if ct.sold_at_drawdown is not None and ct.sold_fraction > 0
                          else f" Crash test: you held on through a {ct.max_drawdown_seen:.0%} fall.")
            game_note += " It is hypothetical and can only make the portfolio more careful."
    elif game is not None:
        game_note = f"Risk game: you chose gamble {game.choice} of 6. It can only make the portfolio more careful."
    else:
        game_note = "Risk Game: not played, so your category rests on your questionnaire answers and your finances alone."
    ibkr, holdings, ibkr_summary = None, body.current_holdings, None
    if stored is not None or body.ibkr_report is not None or body.ibkr_flex is not None:
        catalog = merge_catalog(directory if isinstance(directory, Directory) else None, DEFAULT_CATALOG)
        ibkr = (ibkr_history_from_report(stored[0], as_of, catalog, synthetic) if stored is not None
                else _ibkr_history(body, as_of, catalog, synthetic))
        ibkr_holdings, left_out = _ibkr_holdings(ibkr, {i.ticker for i in catalog})
        if holdings is None:
            holdings = ibkr_holdings
        ibkr_summary = {
            "source": stored[1] if stored is not None else "ibkr_flex" if body.ibkr_flex is not None else "ibkr_report",
            "n_trades": ibkr.n_trades,
            "start": ibkr.start.isoformat(), "end": ibkr.end.isoformat(), "currency": ibkr.currency,
            "valued_at_trade_price": ibkr.valued_at_trade_price, "holdings_left_out": left_out,
            "notes": ibkr.notes + ([f"{len(left_out)} holdings are not in our product list and were left out of "
                                    "your current holdings."] if left_out else []),
        }
    if game is not None or ibkr is not None:
        data = BehavioralData(investor_id=INVESTOR_ID, game_result=game.to_game_result() if game else None,
                              history=ibkr.history if ibkr else None,
                              loss_acceptance=loss_acceptance, crash_test=crash_test,
                              stated_horizon_years=body.horizon_years, data_source="real")
        # The game is played now, after the last close used for prices: let Agent B use it.
        # With the game inside the category Agent B does not tighten on it a second time (its crash test and the trade log stay).
        behavior_options = {"game_in_profile": True} if in_profile else {}
        behavioral = BehavioralTwinAgent(_SingleInvestor(data), BehaviorConfig(accept_game_after_as_of=True, **behavior_options))
    goal = None
    if body.goal_amount is not None:
        goal = GoalSpec(start_amount=goal_start if goal_start is not None else answers.amount_invested,
                        monthly_flow=body.monthly_flow,
                        goal_amount=body.goal_amount, horizon_years=body.horizon_years)
    if not _build_slots.acquire(timeout=settings.build_wait_seconds()):       # no value the client sent in this sentence
        raise HTTPException(503, "The service is busy building other portfolios. Please try again in a minute.",
                            headers={"Retry-After": "30"})
    try:
        run = run_pipeline(
            answers, investor_id=INVESTOR_ID, goal=goal, preferences=preferences, preference_bank=PREF_BANK,
            interest_areas=list(body.interest_areas) if body.interest_areas is not None else None,
            goal_type=answers.choices.get("B1", "other"), horizon_years=body.horizon_years, as_of=as_of,
            trading_platforms=body.trading_platforms, behavioral_agent=behavioral,
            price_source=SyntheticPriceSource() if synthetic else None, bank=BANK, profile_config=PROFILE_CONFIG,
            liquidity_need=body.liquidity_need, min_cash_buffer=body.min_cash_buffer,
            current_holdings=holdings, agent_config=agent_config, risk_game=game_input,
            **({"excluded_assets": list(extra_excluded)} if extra_excluded else {}),
            briefing_builder=_budgeted_briefing if briefing else None,   # EGOQUANT_BRIEFING=live|fixture|off
            directory=directory,                   # None until the background load is done
            max_interest_areas=PORTFOLIO_MAX_INTEREST_AREAS, fetch_seconds=PORTFOLIO_PRICE_FETCH_SECONDS,
        )
    except (PriceSourceError, MarketDataUnavailable) as error:
        logger.warning("Market data unavailable: %s: %s", type(error).__name__, error)
        raise HTTPException(503, f"Market data is unavailable right now ({_market_data_cause(error)}). "
                                 "Please retry in a minute.") from None
    except ValidationError as error:
        raise HTTPException(422, _first_error(error)) from None
    except ValueError as error:
        raise HTTPException(422, _GOAL_ERRORS.get(str(error)) or _preference_error(error)) from None
    finally:
        _build_slots.release()
    return risk, as_of, run, game_note, ibkr, ibkr_summary


def _portfolio_out(risk, as_of, run, game_note, ibkr_summary) -> PortfolioOut:
    report = run.report
    return _with_chat_context(PortfolioOut(
        approved=run.approved, status=run.result.final.status, c_level=risk.c_level, as_of=as_of,
        data_source=report.data_source, violations=plain_violations(list(run.review.violations)), game_note=game_note,
        goal=_goal_summary(run) if run.goal_analysis is not None else None, universe=_universe_summary(run),
        goal_unavailable=run.goal_unavailable,
        fallback_reason=_fallback_label(run.market_report), trading_history=ibkr_summary,
        prices_as_of=getattr(run.market_report, "prices_as_of", None),
        price_note=getattr(run.market_report, "price_note", None),
        snapshot_first=bool(getattr(run.market_report, "snapshot_first", False)),
        checks=[c.model_dump() for c in run.review.checks],
        report=_report_json(run), html=render_html(report), briefing_context=_briefing_context(run),
    ))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _briefing_context(run) -> str | None:
    """Sealed inputs for the full news briefing (/briefing/start): APPROVED weights only (None when rejected),
    interests, watchlist, max R-level, horizon and Agent C's drawdown numbers. Never fails the portfolio."""
    try:
        from egoquant.briefing.final import report_digest
        from egoquant.briefing.jobs import context_facts, seal_briefing_context
        agent_c = run.agent_c
        last = run.market_report
        areas = set(agent_c.interests.interest_areas)
        interest_tickers = ([i.ticker for i in agent_c.catalog if areas & set(i.areas)]
                            + list(agent_c.selection.watchlist_tickers))
        names = {i.ticker: i.name for i in [*run.selection.instruments, *run.selection.watchlist]}
        facts = context_facts(
            interests=agent_c.interests, watchlist=list(agent_c.selection.watchlist_tickers),
            weights=dict(run.result.final.weights) if run.approved else None,
            horizon_years=run.context.horizon_years if hasattr(run.context, "horizon_years") else 10,
            drawdowns=list(last.alerts) if last is not None else [], names=names,
            data_to=last.last_date if last is not None else None, synthetic=run.report.data_source == "synthetic",
            drawdown_threshold=getattr(agent_c, "drawdown_alert_threshold", None), interest_tickers=interest_tickers,
            report_digest=report_digest(_report_json(run)))       # only this report may take the final briefing
        return seal_briefing_context(facts)
    except Exception:
        logger.exception("Briefing context could not be sealed")
        return None


def _report_json(run) -> dict:
    """The ClientReport as JSON, with the monthly view of the SAME briefing fetch beside the weekly one:
    report.market_briefing (week, unchanged shape) and report.market_briefing_month, plus what_changed (week) and
    what_changed_month. Read-only: nothing here changes a number."""
    report = run.report
    out = report.model_dump(mode="json")
    briefing = report.market_briefing
    month = getattr(briefing, "month", None)
    if isinstance(out.get("market_briefing"), dict):
        out["market_briefing"].pop("month", None)
    out["market_briefing_month"] = month.model_dump(mode="json") if month is not None else None
    lines = _what_changed_for(run, month) if month is not None else []
    out["what_changed_month"] = lines
    out["what_changed_month_action"] = "offer_risk_profile_recheck" if lines else None
    return out


def _what_changed_for(run, briefing) -> list[str]:
    """'What changed' for another period's briefing, built exactly as the pipeline builds the weekly one:
    Agent C drawdowns (holdings of an APPROVED proposal first, then interest/watchlist instruments) + the
    briefing's tone alerts. Advice-filtered in what_changed_lines."""
    try:
        agent_c = run.agent_c
        last = run.market_report
        weights = run.result.final.weights if run.approved else {}
        holdings = [t for t, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])) if w > 1e-9]
        areas = set(agent_c.interests.interest_areas)
        interest_tickers = ({i.ticker for i in agent_c.catalog if areas & set(i.areas)}
                            | set(agent_c.selection.watchlist_tickers))
        names = {i.ticker: i.name for i in [*run.selection.instruments, *run.selection.watchlist]}
        return what_changed_lines(list(last.alerts) if last is not None else [], briefing, names,
                                  last.last_date if last is not None else None,
                                  synthetic=run.report.data_source == "synthetic", holdings=holdings,
                                  interest_tickers=interest_tickers,
                                  drawdown_threshold=getattr(agent_c, "drawdown_alert_threshold", None))
    except Exception:                                   # the monthly "What changed" is optional
        logger.exception("Monthly 'What changed' failed")
        return []
def _with_chat_context(out: PortfolioOut) -> PortfolioOut:
    """Seal the facts the advisor chat may discuss, so the stateless chat can trust the browser's copy."""
    out.chat_context = seal_context(build_portfolio_facts(out.model_dump(mode="json", exclude={"html"})))
    return out


def _goal_summary(run) -> dict:
    """What the page needs to show the goal result and how the portfolio was chosen."""
    g, sc, menu = run.goal_analysis, run.screening, run.result.menu
    by_index = {o.menu_index: o for o in g.outcomes}
    shortlist = []
    for o in g.outcomes:                            # best first
        p = menu.portfolios[o.menu_index]
        shortlist.append({
            **o.model_dump(), "label": p.message.split(" | ")[0],
            "vol": p.risk.ex_ante_vol if p.risk else None,
            "stress_loss": max(0.0, -p.stress.get(sc.stress_scenario, 0.0)),
            "chosen": o.menu_index == g.chosen_index,
        })
    return {
        **g.model_dump(mode="json", exclude={"outcomes"}),
        "chosen": by_index[g.chosen_index].model_dump(),
        "n_candidates": len(menu.portfolios),
        "shortlist": shortlist,
        "screening": {"counts": sc.counts, "band_lower": sc.band_lower, "band_upper": sc.band_upper,
                      "facts": sc.facts, "relaxed": sc.relaxed, "ignored": sc.ignored},
    }


def _universe_summary(run) -> dict:
    """Which investments Agent C priced for this investor's interests, and what it left out and why."""
    sel, fetch = run.selection, run.agent_c.last_fetch
    return {
        "interest_areas": list(sel.interest_areas),
        "instruments": [{"ticker": i.ticker, "name": i.name, "asset_class": i.asset_class, "r_level": i.r_level}
                        for i in sel.instruments],
        "not_fetched": [e.model_dump() for e in sel.not_fetched],          # the most relevant few, with why
        "n_not_fetched": fetch.n_not_fetched if fetch else len(sel.not_fetched),
        "deprioritized": len(sel.deprioritized),
        "replaced": dict(fetch.replaced) if fetch else {},
        "from_cache": len(fetch.from_cache) if fetch else 0,
        "new_requests": sum(fetch.new_requests.values()) if fetch else 0,
        "watchlist": [i.ticker for i in sel.watchlist],
    }


# The goal path (Agent A's run_goal_based) raises these when nothing fits; say what the user can change.
_GOAL_ERRORS = {
    "no candidate portfolio satisfies the investor's limits":
        "No portfolio fits all of your limits. A high cash or liquidity share is the usual cause: "
        "try a lower percentage, or choose more interest areas.",
    "Agent B approved no candidate portfolio":
        "None of the candidate portfolios fit how you take risk. Try more interest areas or a longer horizon.",
}


def _not_in_future(value: datetime) -> datetime:
    now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
    if value > now + timedelta(minutes=10):
        raise ValueError("the game time is in the future")
    return value


class _SingleInvestor:
    """Agent B data provider for one request (only the risk game; no trading logs yet)."""

    def __init__(self, data: BehavioralData):
        self._data = data

    def get_behavioral_data(self, investor_id: str) -> BehavioralData | None:
        return self._data if investor_id == self._data.investor_id else None


def _score(answers: Answers, game: GameInput | None = None):
    """The risk profile (docs/RISK_GAME_BLEND.md: with the Risk Game when one was played; none = the questionnaire alone)."""
    game_args = {"game": game, **settings.risk_game_options()} if game is not None else {}
    try:
        return build_risk_profile(answers, BANK, PROFILE_CONFIG, assessed_at=datetime.now(timezone.utc), **game_args)
    except (KeyError, ValueError) as error:
        message = error.args[0] if error.args else "invalid answers"
        raise HTTPException(422, f"Invalid answers: {message}") from None


def _game_fields(risk) -> dict:
    """The optional Risk Game breakdown of a profile, as ProfileOut fields (empty when no game was blended in)."""
    if not getattr(risk, "game_played", False):
        return {}
    return {"game_played": True, "game_score": risk.game_score, "game_level": risk.game_level,
            "blended_level": risk.blended_level, "questionnaire_class": risk.questionnaire_class}


def _first_error(error: ValidationError) -> str:
    """First validation problem as one sentence, without pydantic's "Value error, " prefix."""
    first = error.errors()[0]
    message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"{where}: {message}" if where else message


def _market_data_cause(error: Exception) -> str:
    """A fixed, safe label for why prices failed; the vendor's raw message stays in the server log."""
    if isinstance(error, MarketDataUnavailable):
        return "price data incomplete"
    return _cause_label(str(error))


def _fallback_label(market_report) -> str | None:
    """Why synthetic prices replaced the live feed (EGOQUANT_DATA_FALLBACK), as a safe label, or None."""
    reason = getattr(market_report, "fallback_reason", None)
    return _cause_label(reason) if reason else None


def _cause_label(text: str) -> str:
    if "TIINGO_API_KEY is not set" in text:
        return "Tiingo key not configured"
    if "refused the API key" in text:
        return "Tiingo key rejected"
    if "rate limit" in text:
        return "rate limit reached"
    if "refused the request from this location" in text:
        return "crypto feed blocked in this region"
    if "answered too slowly" in text:
        return "price feed too slow"                  # Agent C's slow-feed guard: the build used the saved snapshot
    return "data vendor error"


def _data_mode() -> Literal["live", "synthetic"]:
    return "synthetic" if os.getenv("EGOQUANT_DATA", "").strip().lower() == "synthetic" else "live"


def _last_weekday_before(day: date) -> date:
    """The last Monday-Friday strictly before `day`: the most recent close that is final."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


_IBKR_MAX_PRICED = 20                  # most traded symbols priced per build (Tiingo budget; others: trade prices)
_IBKR_CASH_ASSET = "BIL"               # the account's cash is compared as Agent A's cash fund (T-bills)
_IBKR_SNAPSHOT_MAX_AGE_DAYS = 30       # a snapshot older than this before the decision date is not used (= Agent C's)


def _ibkr_history(body: PortfolioIn, as_of: date, catalog, synthetic: bool) -> IbkrHistory:
    """The investor's IBKR trades as Agent B's TradingHistory. Nothing is stored; errors never echo the input."""
    report, _ = load_ibkr_report(body.ibkr_report, body.ibkr_flex, body.ibkr_filename)
    return ibkr_history_from_report(report, as_of, catalog, synthetic)


def ibkr_history_from_report(report, as_of: date, catalog, synthetic: bool) -> IbkrHistory:
    """Replay a parsed (or stored) IBKR report with prices from Agent C's cache into Agent B's TradingHistory."""
    try:
        known = {i.ticker: i for i in catalog}
        symbols = sorted({t.symbol for t in report.trades})
        classes = {s: known[s].asset_class for s in symbols if s in known}
        prices = pd.DataFrame()
        if not synthetic:                              # synthetic mode: every day is valued at trade prices
            priceable = [s for s in symbols if s in known and known[s].market == "us_listed"][:_IBKR_MAX_PRICED]
            prices = _ibkr_prices(priceable, report.start, as_of, catalog)
        return to_trading_history(report, prices=prices, asset_classes=classes, as_of=as_of)
    except IbkrReportError as err:
        raise HTTPException(422, f"IBKR report: {err}") from None


def _ibkr_prices(symbols: list[str], start: date, end: date, catalog) -> pd.DataFrame:
    """Daily closes for the traded symbols from Agent C's cached live source; empty on any failure (the
    replay then values those days at trade prices and says so). In snapshot price mode (EGOQUANT_PRICE_MODE) no live request
    is sent at all: the committed snapshot answers directly, like every other part of the build."""
    if not symbols:
        return pd.DataFrame()
    if settings.price_mode() == "snapshot":
        return _ibkr_snapshot_prices(symbols, start, end)
    try:
        return build_live_source({"us_listed"}, catalog).fetch_adjusted_close(symbols, start, end)
    except Exception as exc:                           # rate limit, no key, network: degrade, never fail the build
        logger.warning("IBKR replay prices unavailable: %s", type(exc).__name__)
    return _ibkr_snapshot_prices(symbols, start, end)


def _ibkr_snapshot_prices(symbols: list[str], start: date, end: date) -> pd.DataFrame:
    """The live feed is down: the committed snapshot of REAL prices Agent C falls back to (never mixed into one
    price table with live data: this call is only reached when the live call failed). Empty when the snapshot is
    switched off, unreadable or too old; the replay then values those days at trade prices and says so. Without
    prices Agent B cannot see how far the market had fallen when the investor sold."""
    try:
        if not settings.price_snapshot_enabled():
            return pd.DataFrame()
        snapshot = load_snapshot(str(agent_c_snapshot_path()))
        if (end - snapshot.prices_as_of).days > _IBKR_SNAPSHOT_MAX_AGE_DAYS:
            return pd.DataFrame()
        return snapshot.fetch_adjusted_close(symbols, start, end)
    except Exception as exc:                           # a damaged file must not fail the build either
        logger.warning("IBKR replay snapshot unavailable: %s", type(exc).__name__)
        return pd.DataFrame()


def _ibkr_holdings(history: IbkrHistory, known: set[str]) -> tuple[dict[str, float] | None, list[str]]:
    """Final IBKR positions as current_holdings weights: catalog tickers only, cash as the cash fund."""
    weights = {t: w for t, w in history.final_weights.items() if w > 1e-9}
    cash = weights.pop(CASH_ID, 0.0)
    kept = {t: w for t, w in weights.items() if t in known}
    left_out = sorted(set(weights) - set(kept))
    if cash > 0:
        kept[_IBKR_CASH_ASSET] = kept.get(_IBKR_CASH_ASSET, 0.0) + cash
    total = sum(kept.values())
    if total <= 0:
        return None, left_out
    return {t: w / total for t, w in kept.items()}, left_out


def _directory_for_request(start: bool = True) -> Directory | None:
    """The directory if loaded, else None (curated catalog). Starts a background load when there is none yet
    or the one in memory was checked on an earlier day (load_directory reuses the saved file while fresh)."""
    global _directory_loading, _directory_attempt_at
    with _directory_lock:
        due = _directory is None or _directory_checked_on != date.today()
        recent_try = (_directory_attempt_at is not None
                      and time.monotonic() - _directory_attempt_at < _DIRECTORY_RETRY_SECONDS)
        if start and due and not _directory_loading and not (_directory is None and recent_try):
            _directory_loading, _directory_attempt_at = True, time.monotonic()
            Thread(target=_load_directory_in_background, daemon=True, name="agent-c-directory").start()
        return _directory


def _load_directory_in_background() -> None:
    global _directory, _directory_checked_on, _directory_loading
    try:
        loaded = load_directory()
    except Exception:                                   # never let the thread die silently
        logger.exception("Agent C directory load failed; the curated catalog stays in use")
        loaded = None
    with _directory_lock:
        if loaded is not None:
            _directory, _directory_checked_on = loaded, date.today()
        _directory_loading = False
    logger.info("Agent C directory %s", "ready" if loaded is not None else "unavailable (curated catalog in use)")


def _check_portfolio_rate(timestamps: deque | None = None, limit: int | None = None) -> None:
    timestamps = _portfolio_timestamps if timestamps is None else timestamps
    limit = _PORTFOLIO_LIMIT if limit is None else limit
    now = time.monotonic()
    with _portfolio_lock:
        while timestamps and now - timestamps[0] > _PORTFOLIO_WINDOW:
            timestamps.popleft()
        if len(timestamps) >= limit:
            raise HTTPException(429, "Too many portfolio requests. Please try again later.", headers={"Retry-After": "60"})
        timestamps.append(now)


# --------------------------------------------------------------------------
# Debug endpoint (hidden from API docs, not part of production flow)
# --------------------------------------------------------------------------
_DEBUG_PERSONA_KEY: dict[str, str] = {
    "C1": "no_loss_saver",
    "C2": "cautious_retiree",
    "C3": "balanced_saver",
    "C4": "growth_investor",
    "C5": "crypto_trader",
    "C5b": "eager_beginner",   # C5 but knowledge-restricted (crypto blocked)
}
_VALID_PERSONA_KEYS = set(_DEBUG_PERSONA_KEY.values())


class DebugPortfolioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    c_level: Literal["C1", "C2", "C3", "C4", "C5", "C5b"]
    interest_areas: list[InterestArea] | None = Field(None, max_length=20)
    trading_platforms: list[str] = Field(default_factory=list, max_length=10)
    horizon_years: float = Field(10.0, gt=0, le=60)
    liquidity_need: float = Field(0.0, ge=0, le=1)
    min_cash_buffer: float = Field(0.0, ge=0, le=1)
    goal_amount: float | None = Field(None, gt=0, le=1e12)     # optional: runs the goal path (eval/chat_redteam.py)
    monthly_flow: float = Field(0.0, ge=-1e9, le=1e9)

    @field_validator("trading_platforms")
    @classmethod
    def known_platforms(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_platform(p) for p in value))


def debug_pages_on() -> bool:
    """/debug/* exists only with EGOQUANT_DEBUG_PAGES=on (also 1, true, yes); off in production by default."""
    return os.getenv("EGOQUANT_DEBUG_PAGES", "").strip().lower() in {"1", "true", "on", "yes"}


def _debug_gate() -> None:
    """Runs before the body is read, so a switched-off debug page is a 404 for any body, not a 422 that shows it exists."""
    if not debug_pages_on():
        raise HTTPException(404, "Not Found")


@router.post("/debug/portfolio", include_in_schema=False, dependencies=[Depends(_debug_gate)])
def debug_portfolio(body: DebugPortfolioIn):
    """Debug: full pipeline with demo persona answers for the given C-level.
    Always uses SyntheticPriceSource — no Tiingo key required.
    C5b = eager_beginner: same score as C5 but crypto blocked by knowledge restriction.
    """
    if not debug_pages_on():
        raise HTTPException(404, "Not Found")                # as if the route did not exist
    _check_portfolio_rate(_debug_timestamps, _DEBUG_LIMIT)   # public and unauthenticated: own, smaller bucket
    persona = _DEMO_PERSONAS[_DEBUG_PERSONA_KEY[body.c_level]]
    answers = Answers(
        questionnaire_version=BANK.questionnaire_version,
        choices=persona.choices,
        multi_choices={"K4": persona.k4},
        amount_invested=persona.amount_invested,
        amount_needed=persona.amount_needed,
    )
    risk = _score(answers)
    # No areas given: the persona's own preference-form answers decide (as in run_persona)
    interest_areas = list(body.interest_areas) if body.interest_areas else None
    preferences = None if interest_areas else persona.preference_answers()
    as_of = _last_weekday_before(date.today())
    try:
        run = run_pipeline(
            answers, investor_id=INVESTOR_ID,
            interest_areas=interest_areas, preferences=preferences,
            goal_type=answers.choices.get("B1", "other"),
            horizon_years=body.horizon_years,
            as_of=as_of,
            trading_platforms=body.trading_platforms,
            behavioral_agent=None,
            price_source=SyntheticPriceSource(),
            bank=BANK, profile_config=PROFILE_CONFIG,
            liquidity_need=body.liquidity_need,
            min_cash_buffer=body.min_cash_buffer,
            current_holdings=None,
            briefing_builder=build_web_briefing,   # same news briefing as /portfolio (EGOQUANT_BRIEFING decides)
            goal=(GoalSpec(start_amount=answers.amount_invested, monthly_flow=body.monthly_flow,
                           goal_amount=body.goal_amount, horizon_years=body.horizon_years)
                  if body.goal_amount is not None else None),
        )
    except ValidationError as error:
        raise HTTPException(422, _first_error(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    report = run.report
    out = _with_chat_context(PortfolioOut(
        approved=run.approved, status=run.result.final.status, c_level=risk.c_level, as_of=as_of,
        data_source=report.data_source, violations=list(run.review.violations), game_note=None,
        goal=_goal_summary(run) if run.goal_analysis is not None else None, goal_unavailable=run.goal_unavailable,
        checks=[c.model_dump() for c in run.review.checks],
        report=_report_json(run), html=render_html(report), briefing_context=_briefing_context(run),
    ))
    # The same sealed what-if inputs as /portfolio (eval/chat_redteam.py's what-if cases use them).
    inputs = PortfolioIn(
        answers=AnswersIn(choices=persona.choices, multi_choices={"K4": persona.k4},
                          amount_invested=persona.amount_invested, amount_needed=persona.amount_needed),
        preferences=(PreferenceAnswersIn(preferences_version=preferences.preferences_version,
                                         choices=preferences.choices) if preferences is not None else None),
        interest_areas=interest_areas, trading_platforms=body.trading_platforms, horizon_years=body.horizon_years,
        liquidity_need=body.liquidity_need, min_cash_buffer=body.min_cash_buffer,
        goal_amount=body.goal_amount, monthly_flow=body.monthly_flow)
    out.what_if_context = _what_if_context(inputs, None, run, as_of, None)
    return out


router.include_router(account_router)   # /account/... (signed-in users; egoquant/accounts)
