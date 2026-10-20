"""Demo trader profiles: one complete investor each, from questionnaire answers to current holdings.

Each persona is what the app would collect for one person: the formal questionnaire (answers to the
real question bank, egoquant/risk_profile/questions.json), the investment-preferences form that follows
it (egoquant/preferences/questions.json; only options their profile allows), the Advisor chatbot's
validated fields (trading platforms, goal, horizon, cash needs) and the current portfolio.
`run_persona` sends it through the whole pipeline. With the default price source the portfolio is
built from REAL market data (Tiingo for US-listed funds, Binance for crypto spot pairs).

The people are INVENTED. Personas with `synthetic_behavior` borrow a SYNTHETIC trading history and
risk-game result from egoquant.demo.synthetic_investors so Agent B has evidence to work with; Agent B
labels that evidence "synthetic" in its report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from egoquant.agents.behavioral import BehavioralTwinAgent, NoBehavioralEvidence
from egoquant.agents.behavioral.models import BehavioralData
from egoquant.agents.market_intel import PriceSource
from egoquant.demo.synthetic_investors import build_synthetic_data
from egoquant.models import AgentConfig, BehavioralAdvisor, RiskClass
from egoquant.pipeline import PipelineResult, run_pipeline
from egoquant.preferences import PreferenceAnswers, build_form, load_preference_bank, to_interest_fields
from egoquant.risk_profile import Answers, GameInput, build_risk_profile, load_all, load_question_bank


@dataclass(frozen=True)
class Persona:
    key: str
    investor_id: str
    description: str
    expected_class: RiskClass                     # what the questionnaire answers score (checked by tests)
    choices: dict[str, str]                       # single-choice answers, question id -> option id
    k4: list[str]                                 # products invested in before (multi-select K4)
    amount_invested: float
    amount_needed: float
    preferences: dict[str, list[str]]             # answers to the preference form, question id -> option ids
    trading_platforms: list[str]
    goal_type: str
    horizon_years: float
    min_cash_buffer: float = 0.0
    liquidity_need: float = 0.0
    current_holdings: dict[str, float] | None = None
    synthetic_behavior: str | None = None         # key in egoquant.demo.synthetic_investors.SYNTHETIC_PROFILES
    # The Risk Game this person played (docs/RISK_GAME_BLEND.md): {"tolerance": 0-100, "loss_acceptance": 0-100 or absent}.
    # None = not played, which is every persona unless a test or the demo pack says otherwise: nothing moves by default.
    risk_game: dict[str, int] | None = None

    def answers(self) -> Answers:
        return Answers(questionnaire_version=load_question_bank().questionnaire_version, choices=self.choices,
                       multi_choices={"K4": self.k4}, amount_invested=self.amount_invested,
                       amount_needed=self.amount_needed)

    def game(self) -> GameInput | None:
        """The persona's Risk Game result, or None when it did not play."""
        return None if self.risk_game is None else GameInput(**self.risk_game)

    def preference_answers(self) -> PreferenceAnswers:
        return PreferenceAnswers(preferences_version=load_preference_bank().preferences_version,
                                 choices=self.preferences)


_FULL_KNOWLEDGE = {"K1": "more", "K2": "less", "K3": "false"}

PERSONAS: dict[str, Persona] = {p.key: p for p in [
    Persona(
        key="no_loss_saver", investor_id="mei", expected_class="C1",
        description="Saving for a flat deposit in two years and will not accept any loss (gate question).",
        choices={"GL1": "d", "GL2": "a", "GL3": "a", "GL4": "a", "GL5": "a", "GL6": "a", "GL7": "a", "GL8": "a",
                 "GL9": "a", "GL10": "a", "GL11": "a", "GL12": "a", "GL13": "a",
                 "B1": "house", "B2": "y1_3", "B4": "m3_6", "B5": "gt75", "B6": "stable",
                 **_FULL_KNOWLEDGE, "K5": "lt1", "D1": "no"},
        k4=["deposits"], amount_invested=40_000, amount_needed=40_000,
        preferences={"P1": ["cash"]}, trading_platforms=["Fidelity"], goal_type="house", horizon_years=2,
    ),
    Persona(
        key="cautious_retiree", investor_id="robert", expected_class="C2",
        description="Retired, lives partly on this money, wants steady income and asked about gold.",
        choices={"GL1": "c", "GL2": "a", "GL3": "b", "GL4": "b", "GL5": "b", "GL6": "a", "GL7": "a", "GL8": "b",
                 "GL9": "a", "GL10": "a", "GL11": "b", "GL12": "a", "GL13": "b",
                 "B1": "retirement", "B2": "y3_5", "B4": "gt6", "B5": "p50_75", "B6": "none",
                 **_FULL_KNOWLEDGE, "K5": "gt5", "D1": "yes"},
        k4=["deposits", "bonds"], amount_invested=200_000, amount_needed=170_000,
        preferences={"P1": ["bonds", "cash"], "P6": ["government_bonds", "inflation_protected"], "P7": ["funds"]},
        trading_platforms=["Fidelity"], goal_type="retirement",
        horizon_years=5, min_cash_buffer=0.10, current_holdings={"BIL": 0.50, "AGG": 0.50},
    ),
    Persona(
        key="balanced_saver", investor_id="sara", expected_class="C3",
        description="Mid-career saver with a balanced attitude who wants stocks, bonds and gold through "
                    "funds. Borrows the synthetic 'sara' history (one panic sale, cautious game).",
        choices={"GL1": "b", "GL2": "b", "GL3": "c", "GL4": "b", "GL5": "b", "GL6": "b", "GL7": "a", "GL8": "b",
                 "GL9": "a", "GL10": "b", "GL11": "b", "GL12": "b", "GL13": "b",
                 "B1": "growth", "B2": "gt5", "B4": "m3_6", "B5": "p25_50", "B6": "stable",
                 **_FULL_KNOWLEDGE, "K5": "y3_5", "D1": "yes"},
        k4=["stocks", "bonds"], amount_invested=50_000, amount_needed=30_000,
        preferences={"P1": ["stocks", "bonds", "gold"], "P3": ["us_stocks", "dividend_stocks"], "P4": ["funds"],
                     "P6": ["no_preference"]},
        trading_platforms=["Interactive Brokers"],
        goal_type="growth", horizon_years=10, min_cash_buffer=0.05,
        current_holdings={"VT": 0.30, "AGG": 0.50, "BIL": 0.20}, synthetic_behavior="sara",
    ),
    Persona(
        key="growth_investor", investor_id="daniel", expected_class="C4",
        description="Long horizon, stable income; wants a mix of funds and individual stocks in technology "
                    "and healthcare, plus real estate, gold and commodities, and avoids energy. Borrows the "
                    "synthetic 'steady' history (held through two crashes).",
        choices={"GL1": "b", "GL2": "c", "GL3": "c", "GL4": "c", "GL5": "c", "GL6": "b", "GL7": "b", "GL8": "c",
                 "GL9": "a", "GL10": "b", "GL11": "b", "GL12": "b", "GL13": "b",
                 "B1": "growth", "B2": "gt5", "B4": "gt6", "B5": "lt25", "B6": "stable",
                 **_FULL_KNOWLEDGE, "K5": "gt5", "D1": "yes"},
        k4=["stocks", "bonds"], amount_invested=100_000, amount_needed=0,
        preferences={"P1": ["stocks", "real_estate", "gold", "commodities"], "P2": ["technology", "healthcare"],
                     "P4": ["mix"], "P5": ["energy"]},
        trading_platforms=["Charles Schwab"], goal_type="growth", horizon_years=15, min_cash_buffer=0.02,
        synthetic_behavior="steady",
    ),
    Persona(
        key="crypto_trader", investor_id="alex", expected_class="C5",
        description="Experienced, risk-seeking trader on Interactive Brokers and Binance who wants "
                    "individual tech stocks, gold, bitcoin and other coins. Borrows the synthetic 'overtrader' "
                    "history.",
        choices={"GL1": "a", "GL2": "d", "GL3": "d", "GL4": "c", "GL5": "c", "GL6": "d", "GL7": "c", "GL8": "d",
                 "GL9": "b", "GL10": "b", "GL11": "c", "GL12": "c", "GL13": "c",
                 "B1": "growth", "B2": "gt5", "B4": "gt6", "B5": "lt25", "B6": "stable",
                 **_FULL_KNOWLEDGE, "K5": "gt5", "D1": "yes"},
        k4=["stocks", "crypto", "derivatives"], amount_invested=30_000, amount_needed=0,
        preferences={"P1": ["stocks", "gold", "crypto", "altcoins"], "P2": ["technology"], "P4": ["individual"]},
        trading_platforms=["Interactive Brokers", "Binance"], goal_type="growth", horizon_years=10,
        min_cash_buffer=0.02, current_holdings={"QQQ": 0.50, "VTI": 0.30, "BIL": 0.20},
        synthetic_behavior="overtrader",
    ),
    Persona(
        key="eager_beginner", investor_id="lina", expected_class="C5",
        description="Bold answers but no investing experience and weak financial knowledge: crypto is not "
                    "offered on her preference form (knowledge restriction), so she picks US stocks.",
        choices={"GL1": "a", "GL2": "d", "GL3": "d", "GL4": "c", "GL5": "c", "GL6": "d", "GL7": "c", "GL8": "d",
                 "GL9": "b", "GL10": "b", "GL11": "c", "GL12": "c", "GL13": "c",
                 "B1": "growth", "B2": "gt5", "B4": "gt6", "B5": "lt25", "B6": "stable",
                 "K1": "dk", "K2": "dk", "K3": "true", "K5": "none", "D1": "yes"},
        k4=["none"], amount_invested=10_000, amount_needed=0,
        preferences={"P1": ["stocks"], "P3": ["us_stocks"], "P4": ["funds"]},
        trading_platforms=["Robinhood"], goal_type="growth",
        horizon_years=10, min_cash_buffer=0.05,
    ),
]}


class PersonaBehavioralProvider:
    """Agent B data for personas: the synthetic history named by each persona, under its investor id."""

    def __init__(self, seed: int = 11):
        synthetic = build_synthetic_data(seed)
        self._data: dict[str, BehavioralData] = {
            p.investor_id: synthetic[p.synthetic_behavior].model_copy(update={"investor_id": p.investor_id})
            for p in PERSONAS.values() if p.synthetic_behavior
        }

    def get_behavioral_data(self, investor_id: str) -> BehavioralData | None:
        return self._data.get(investor_id)


def behavioral_agent_for(persona: Persona) -> BehavioralAdvisor:
    """The real Agent B when the persona has (synthetic) behavior data, else the no-evidence agent."""
    return BehavioralTwinAgent(PersonaBehavioralProvider()) if persona.synthetic_behavior else NoBehavioralEvidence()


def interest_fields(persona: Persona | str) -> dict:
    """The persona's preference answers as Agent C's InvestorInterests fields, checked against the form
    their profile gets (the same derivation run_pipeline does for preferences)."""
    p = PERSONAS[persona] if isinstance(persona, str) else persona
    bank, config = load_all()
    pref_bank = load_preference_bank()
    profile = build_risk_profile(p.answers(), bank, config, assessed_at=datetime.now(timezone.utc), game=p.game())
    form = build_form(pref_bank, profile, config, p.trading_platforms)
    return to_interest_fields(p.preference_answers(), form, pref_bank)


def run_persona(persona: Persona | str, as_of: date, *, price_source: PriceSource | None = None,
                agent_config: AgentConfig | None = None, behavioral_agent: BehavioralAdvisor | None = None,
                briefing_builder=None, briefing_period: str = "week") -> PipelineResult:
    """Full pipeline for one persona. price_source=None means REAL data (Tiingo + Binance).
    briefing_builder / briefing_period: the optional Agent D briefing, passed straight to run_pipeline."""
    p = PERSONAS[persona] if isinstance(persona, str) else persona
    return run_pipeline(
        p.answers(), investor_id=p.investor_id, interest_areas=None, preferences=p.preference_answers(),
        goal_type=p.goal_type,
        horizon_years=p.horizon_years, as_of=as_of, trading_platforms=p.trading_platforms,
        behavioral_agent=behavioral_agent or behavioral_agent_for(p), price_source=price_source,
        agent_config=agent_config, min_cash_buffer=p.min_cash_buffer, liquidity_need=p.liquidity_need,
        current_holdings=p.current_holdings, briefing_builder=briefing_builder, briefing_period=briefing_period,
    )
