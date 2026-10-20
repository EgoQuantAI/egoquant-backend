"""Investment preferences, asked after the risk questionnaire. Not scored.

    load_preference_bank()                    the questions (questions.json)
    build_form(bank, profile, config, ...)    what THIS investor may choose (JSON for the frontend)
    to_interest_fields(answers, form, bank)   their choices -> Agent C's InvestorInterests fields
"""

from __future__ import annotations

import json
from pathlib import Path

from egoquant.preferences.form import AREA_PRIORITY, build_form, to_interest_fields, validate_answers
from egoquant.preferences.models import FormOption, FormQuestion, PreferenceAnswers, PreferenceBank, PreferenceForm

QUESTIONS_PATH = Path(__file__).parent / "questions.json"


def load_preference_bank(path: Path | str = QUESTIONS_PATH) -> PreferenceBank:
    with open(path, encoding="utf-8") as fh:
        return PreferenceBank.model_validate(json.load(fh))


__all__ = [
    "AREA_PRIORITY", "FormOption", "FormQuestion", "PreferenceAnswers", "PreferenceBank", "PreferenceForm",
    "build_form", "load_preference_bank", "to_interest_fields", "validate_answers",
]
