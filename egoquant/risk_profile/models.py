"""Data contracts owned by the risk questionnaire module.

These live here, not in egoquant/models.py, because that file is shared and changes only by team
agreement (same convention as egoquant/agents/behavioral/models.py). The module talks to the rest of the
system only through the shared Constraint type (see adapter.py):

    Questionnaire -> Answers -> scoring.build_risk_profile -> RiskProfile
    RiskProfile   -> adapter.to_constraints -> list[Constraint]   (source="suitability", to Agent A)
    RiskProfile   -> adapter.to_agent_c_limits                    (max_r_level, blocked product types)

Agent A never receives a RiskProfile or the score: it gets the C-level and the numeric limits only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from egoquant.models import RISK_CLASS_ORDER, RiskClass

ProductAccess = Literal["full", "warning", "blocked_complex"]
GAME_FIELDS = ("game_played", "game_score", "game_level", "blended_level", "questionnaire_class")
BindingDimension = Literal["tolerance", "capacity", "gate", "game"]
QuestionType = Literal["single", "multi", "number"]


# --------------------------------------------------------------------------
# Question bank (questions.json)
# --------------------------------------------------------------------------
class Option(BaseModel):
    id: str
    text: str
    points: int | None = None                        # Grable & Lytton items only


class Question(BaseModel):
    id: str
    section: str
    type: QuestionType
    text: str
    hint: str | None = None                          # our reading aid above the question; never part of the scale
    options: list[Option] = Field(default_factory=list)
    scored: bool = True
    correct_option: str | None = None                # Big Three items only

    def option(self, option_id: str) -> Option:
        for opt in self.options:
            if opt.id == option_id:
                return opt
        raise ValueError(f"{self.id}: unknown option '{option_id}'")


class QuestionBank(BaseModel):
    questionnaire_version: str
    questions: list[Question]

    @model_validator(mode="after")
    def _unique_ids(self) -> "QuestionBank":
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self

    def get(self, question_id: str) -> Question:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)

    def section(self, section: str) -> list[Question]:
        return [q for q in self.questions if q.section == section]


# --------------------------------------------------------------------------
# Scoring configuration (config.yaml). Validated so a typo fails at load time, not mid-scoring.
# --------------------------------------------------------------------------
class ToleranceBand(BaseModel):
    min: int
    max: int
    level: int = Field(ge=1, le=5)
    label: str


class ConsistencyConfig(BaseModel):
    self_item: str
    scenario_items: list[str] = Field(min_length=1)
    excluded_items: list[str] = Field(default_factory=list)
    threshold: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _disjoint(self) -> "ConsistencyConfig":
        if self.self_item in self.scenario_items or set(self.scenario_items) & set(self.excluded_items):
            raise ValueError("self_item, scenario_items and excluded_items must not overlap")
        return self


class ToleranceConfig(BaseModel):
    bands: list[ToleranceBand]
    consistency: ConsistencyConfig

    @model_validator(mode="after")
    def _bands_contiguous(self) -> "ToleranceConfig":
        bands = sorted(self.bands, key=lambda b: b.min)
        for prev, nxt in zip(bands, bands[1:]):
            if nxt.min != prev.max + 1:
                raise ValueError("tolerance bands must be contiguous with no gaps or overlaps")
        return self


class LossCapacityBand(BaseModel):
    below: float = Field(gt=0, le=1)
    cap: int = Field(ge=1, le=5)


class AggregateBand(BaseModel):
    below: float = Field(gt=0)                       # average points below this value (lower bound inclusive) ...
    level: int = Field(ge=1, le=5)                   # ... give this capacity level


class CapacityAggregate(BaseModel):
    """The capacity answers that are AVERAGED like the risk-attitude items are summed: each answer gives points, the
    average of the points decides the level, so one weak answer is outweighed by strong ones."""

    questions: list[str]
    option_points: dict[str, dict[str, int]]         # per averaged question (B3 uses its loss-capacity band as points)
    level_bands: list[AggregateBand]
    max_level: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def _check(self) -> "CapacityAggregate":
        belows = [b.below for b in self.level_bands]
        if belows != sorted(belows):
            raise ValueError("aggregate level_bands must be in increasing order")
        for qid, points in self.option_points.items():
            if any(not 1 <= v <= 5 for v in points.values()):
                raise ValueError(f"{qid}: points must be between 1 and 5")
        return self


class CapacityConfig(BaseModel):
    """Risk capacity (section B). A question is either a HARD cap (its level limits the capacity on its own) or part of
    the AVERAGE (see CapacityAggregate); capacity_level = min(hard caps, level of the average)."""

    question_order: list[str]
    hard_caps: list[str] = Field(default_factory=list)
    option_caps: dict[str, dict[str, int]] = Field(default_factory=dict)   # hard-capped questions other than B3
    # An answer to an AVERAGED question that limits the capacity on its own, however good the other answers are (question ->
    # option -> level): a horizon under a year is not made safe by big savings. Other options of that question only count
    # as points in the average.
    answer_caps: dict[str, dict[str, int]] = Field(default_factory=dict)
    aggregate: CapacityAggregate
    loss_capacity_bands: list[LossCapacityBand]
    loss_capacity_max_cap: int = Field(ge=1, le=5)
    explain_phrases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "CapacityConfig":
        belows = [b.below for b in self.loss_capacity_bands]
        if belows != sorted(belows):
            raise ValueError("loss_capacity_bands must be in increasing order")
        for qid, caps in {**self.option_caps, **self.answer_caps}.items():
            if any(not 1 <= c <= 5 for c in caps.values()):
                raise ValueError(f"{qid}: caps must be between 1 and 5")
        for qid in self.answer_caps:
            if qid not in self.aggregate.questions or qid == "B3":
                raise ValueError(f"{qid} has answer_caps but is not an averaged answer question (use hard_caps for it)")
        averaged = set(self.aggregate.questions)
        both = averaged & set(self.hard_caps)
        neither = set(self.question_order) - averaged - set(self.hard_caps)
        if both or neither:
            raise ValueError("every capacity question is either a hard cap or averaged, not both and not neither "
                             f"(both: {sorted(both)}, neither: {sorted(neither)})")
        for qid in self.hard_caps:
            if qid != "B3" and qid not in self.option_caps:
                raise ValueError(f"{qid} is a hard cap but has no option_caps")
        for qid in averaged:
            if qid != "B3" and qid not in self.aggregate.option_points:
                raise ValueError(f"{qid} is averaged but has no option_points")
        return self


class KnowledgeConfig(BaseModel):
    big_three: list[str]
    full_access_min_score: int = Field(ge=0, le=3)
    full_access_requires_experience: bool = True
    warning_min_score: int = Field(ge=0, le=3)
    experience_products: list[str]
    no_experience_years: list[str]
    complex_product_types: list[str]


class GateConfig(BaseModel):
    question: str
    no_loss_option: str
    max_stress_loss: float = Field(ge=0, le=1)


class CLevelPolicy(BaseModel):
    max_r_level: int = Field(ge=1, le=5)
    max_equity: float = Field(ge=0, le=1)
    max_commodity: float = Field(ge=0, le=1)
    max_crypto: float = Field(ge=0, le=1)
    illustrative_crash_loss: float = Field(ge=0, le=1)


class ComplianceConfig(BaseModel):
    validity_months: int = Field(gt=0, le=60)


class AdapterConfig(BaseModel):
    stress_scenario: str
    r1_max_asset_weight: float = Field(gt=0, le=1)


class GameConfig(BaseModel):
    """How the Risk Game enters the category (docs/RISK_GAME_BLEND.md). weight = the game's share of the blended tolerance
    (0 = the game is ignored); may_raise False = the game can only lower the tolerance part."""

    weight: float = Field(0.3333333333, ge=0, le=0.5)
    may_raise: bool = True


class RiskProfileConfig(BaseModel):
    questionnaire_version: str
    tolerance: ToleranceConfig
    capacity: CapacityConfig
    knowledge: KnowledgeConfig
    gate: GateConfig
    c_levels: dict[RiskClass, CLevelPolicy]
    adapter: AdapterConfig
    compliance: ComplianceConfig
    game: GameConfig = Field(default_factory=GameConfig)

    @model_validator(mode="after")
    def _c_levels_complete_and_monotone(self) -> "RiskProfileConfig":
        if set(self.c_levels) != set(RISK_CLASS_ORDER):
            raise ValueError("c_levels must define exactly C1..C5")
        for field_name in ("max_r_level", "max_equity", "max_commodity", "max_crypto", "illustrative_crash_loss"):
            values = [getattr(self.c_levels[c], field_name) for c in RISK_CLASS_ORDER]
            if values != sorted(values):
                raise ValueError(f"c_levels.{field_name} must not decrease from C1 to C5")
        return self


# --------------------------------------------------------------------------
# Input: one completed questionnaire
# --------------------------------------------------------------------------
class Answers(BaseModel):
    """Raw answers, by question id. Validated against the question bank by scoring.validate_answers."""

    questionnaire_version: str
    choices: dict[str, str]                          # single-choice items: question_id -> option_id
    multi_choices: dict[str, list[str]] = Field(default_factory=dict)  # multi-select items (K4)
    amount_invested: float = Field(gt=0)             # B3_invested
    amount_needed: float = Field(ge=0)               # B3_needed

    @model_validator(mode="after")
    def _needed_within_invested(self) -> "Answers":
        # Almost always a typing mistake; scoring it would silently place the investor in C1.
        if self.amount_needed > self.amount_invested:
            raise ValueError(
                f"amount needed back by the goal date ({self.amount_needed:,.0f}) cannot be larger than "
                f"the amount invested ({self.amount_invested:,.0f}); please check both numbers"
            )
        return self


class GameInput(BaseModel):
    """What the Risk Game reports (hypothetical, client-reported, like the questionnaire answers): the two scores of
    the game, 0-100 each. `loss_acceptance` is absent when the second part of the game was not played."""

    model_config = ConfigDict(extra="forbid")
    tolerance: int = Field(ge=0, le=100)
    loss_acceptance: int | None = Field(None, ge=0, le=100)


class AnswerAuditRecord(BaseModel):
    """One line of the audit log: what was answered, to which version of which question, and when."""

    question_id: str
    questionnaire_version: str
    option_id: str | None                            # None for numeric items
    value: float | None = None                       # numeric items only (B3)
    timestamp: datetime


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
class RiskProfile(BaseModel):
    tolerance_score: int = Field(ge=13, le=47)
    tolerance_level: int = Field(ge=1, le=5)
    capacity_level: int = Field(ge=1, le=5)
    capacity_binding_question: str                   # the hard cap (B3) that limits it, else the weakest averaged answer
    # average points of the averaged capacity answers (1-5); kept out of the JSON so that a profile serializes as before
    capacity_average: float | None = Field(None, exclude=True)
    loss_capacity: float = Field(ge=0, le=1)
    knowledge_score: int = Field(ge=0, le=3)
    product_access: ProductAccess
    no_loss_gate: bool                               # True = investor accepts no loss (D1 = "no")
    c_level: RiskClass
    lowest_category: bool                            # True only when the gate placed the investor in C1
    binding_dimension: BindingDimension
    max_r_level: int = Field(ge=1, le=5)
    suitability_max_equity: float = Field(ge=0, le=1)
    flags: list[str]
    questionnaire_version: str
    # Compliance record: when the assessment was made, until when it is valid, and a fingerprint of the
    # exact answers it was computed from (same answers -> same hash).
    assessed_at: datetime                            # timezone-aware, UTC; supplied by the caller
    valid_until: datetime                            # assessed_at + compliance.validity_months
    answers_hash: str                                # "sha256:<hex>" of the normalized answers + version

    # The Risk Game inside the category (docs/RISK_GAME_BLEND.md). Present in the JSON ONLY when a game was given: without one
    # the profile serializes exactly as before. (One serializer on this class; pydantic keeps only the last one.)
    game_played: bool = False
    game_score: float | None = None                  # mean of the game's tolerance and loss acceptance (0-100)
    game_level: int | None = Field(None, ge=1, le=5)
    blended_level: int | None = Field(None, ge=1, le=5)
    questionnaire_class: str | None = None           # the category WITHOUT the game, "C1".."C5"

    @model_serializer(mode="wrap")
    def _omit_the_game_when_it_was_not_played(self, handler):
        data = handler(self)
        if not self.game_played:
            for key in GAME_FIELDS:
                data.pop(key, None)
        return data
