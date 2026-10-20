"""Data contracts for the investment-preferences step (asked after the risk questionnaire).

    questions.json --load_preference_bank--> PreferenceBank          (all questions and options)
    PreferenceBank + RiskProfile --build_form--> PreferenceForm      (what THIS investor is offered)
    PreferenceAnswers + PreferenceForm --to_interest_fields--> Agent C's InvestorInterests fields

Not scored. The answers decide what Agent C looks at; they never change the C-level or a limit.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

QuestionType = Literal["single", "multi"]


# --------------------------------------------------------------------------
# Question bank (questions.json)
# --------------------------------------------------------------------------
class PreferenceOption(BaseModel):
    id: str
    text: str
    areas: list[str] = Field(default_factory=list)     # Agent C interest areas this option adds
    style: str | None = None                           # P4: "funds" | "individual" | "mix"
    sector: str | None = None                          # P5: industry to avoid
    individual_bonds: bool | None = None               # P7


class PreferenceQuestion(BaseModel):
    id: str
    type: QuestionType
    required: bool = False
    text: str
    requires: dict[str, str] | None = None             # e.g. {"P1": "stocks"}: only asked if that was picked
    options: list[PreferenceOption]

    def option(self, option_id: str) -> PreferenceOption:
        for o in self.options:
            if o.id == option_id:
                return o
        raise ValueError(f"{self.id}: unknown option '{option_id}'")


class PreferenceBank(BaseModel):
    preferences_version: str
    note: str = ""
    questions: list[PreferenceQuestion]

    @model_validator(mode="after")
    def _unique_ids(self) -> "PreferenceBank":
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("preference question ids must be unique")
        return self

    def get(self, question_id: str) -> PreferenceQuestion:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)


# --------------------------------------------------------------------------
# The form one investor sees (JSON for the frontend)
# --------------------------------------------------------------------------
class FormOption(BaseModel):
    id: str
    text: str
    available: bool
    reason: str | None = None                          # why it is not offered (shown greyed out)


class FormQuestion(BaseModel):
    id: str
    type: QuestionType
    required: bool
    text: str
    requires: dict[str, str] | None = None
    options: list[FormOption]


class PreferenceForm(BaseModel):
    preferences_version: str
    c_level: str
    max_r_level: int
    questions: list[FormQuestion]

    def question(self, question_id: str) -> FormQuestion:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)


# --------------------------------------------------------------------------
# The investor's answers
# --------------------------------------------------------------------------
class PreferenceAnswers(BaseModel):
    """Option ids per question. Single-choice questions take a one-item list."""

    preferences_version: str
    choices: dict[str, list[str]]
