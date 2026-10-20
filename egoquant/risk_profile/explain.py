"""Plain-language sentences for the results page. Deterministic templates, no LLM.

The LLM advisor may rephrase these later, but the facts in them come only from the RiskProfile.
"""

from __future__ import annotations

from egoquant.risk_profile.models import Answers, QuestionBank, RiskProfile, RiskProfileConfig
from egoquant.risk_profile.scoring import B3_ID, tolerance_band


def _money(x: float) -> str:
    return f"{x:,.0f}"


def _capacity_reason(profile: RiskProfile, answers: Answers, bank: QuestionBank,
                     config: RiskProfileConfig) -> str:
    qid = profile.capacity_binding_question
    phrase = config.capacity.explain_phrases.get(qid, qid)
    if qid == B3_ID:
        return f"{phrase} (you can lose at most {profile.loss_capacity:.0%} of it)"
    option_text = bank.get(qid).option(answers.choices[qid]).text
    if qid in config.capacity.hard_caps or answers.choices[qid] in config.capacity.answer_caps.get(qid, {}):
        return f"{phrase} ({option_text.lower()})"                # this answer limits the category on its own
    # An averaged answer: say that the answers count together and which one holds the average back most.
    return (f"your financial situation taken together (horizon, savings, share of wealth invested and income), "
            f"held back most by {phrase} ({option_text.lower()})")


def explain(profile: RiskProfile, answers: Answers, bank: QuestionBank, config: RiskProfileConfig) -> list[str]:
    lines: list[str] = []
    band = tolerance_band(profile.tolerance_score, config)
    lines.append(f"Your attitude to risk scored {profile.tolerance_score} out of 47: "
                 f"attitude level {profile.tolerance_level} of 5 ({band.label}).")

    if profile.game_played and profile.binding_dimension != "gate":
        weight = config.game.weight
        split = ("two thirds / one third" if abs(weight - 1 / 3) < 1e-6
                 else f"{round((1 - weight) * 100)}% / {round(weight * 100)}%")
        lines.append(f"The Risk Game you played came to level {profile.game_level} of 5 (score {profile.game_score:.0f} out of 100). "
                     f"Together with your questionnaire ({split}) that is attitude level {profile.blended_level}. "
                     f"Your financial situation sets a cap at level {profile.capacity_level}. "
                     f"Your category is the lower of the two: {profile.c_level}.")

    if profile.binding_dimension == "gate":
        lines.append("You said you could not accept your investment going down in value, so you are placed in the lowest "
                     "risk category (C1) and only products with the lowest product risk level (R1) are offered.")
    elif profile.binding_dimension == "game":
        was = profile.questionnaire_class
        direction = "more careful" if int(profile.c_level[1:]) < int(was[1:]) else "bolder"
        lines.append(f"The game moved your category: your questionnaire answers alone would give {was}; how you played points "
                     f"to a {direction} attitude, so it is {profile.c_level}.")
    elif profile.binding_dimension == "capacity":
        reason = _capacity_reason(profile, answers, bank, config)
        level = profile.blended_level if profile.game_played else profile.tolerance_level
        lines.append(f"Your attitude to risk is at level {level}, but because of {reason}, "
                     f"your risk category is limited to {profile.c_level}.")
    elif profile.game_played:
        lines.append(f"The game did not change your category: your questionnaire attitude level "
                     f"({profile.tolerance_level}) sets it, {profile.c_level}.")
    else:
        lines.append(f"Your financial situation allows level {profile.capacity_level}, so your attitude level "
                     f"({profile.tolerance_level}) sets your risk category: {profile.c_level}.")

    crash = config.c_levels[profile.c_level].illustrative_crash_loss
    invested = answers.amount_invested
    lines.append(f"In a severe market fall (hypothetical), a {profile.c_level} portfolio of your {_money(invested)} could fall "
                 f"to about {_money(invested * (1 - crash))} (illustrative estimate, not a forecast).")

    if profile.product_access == "warning":
        lines.append("Some of your answers on investment basics were incorrect, so complex products such as "
                     "crypto or leveraged funds need your explicit acknowledgement of the risks first.")
    elif profile.product_access == "blocked_complex":
        lines.append("Based on your answers on investment basics, complex products such as crypto or "
                     "leveraged funds are not available to you for now.")

    if "review_suggested" in profile.flags:
        lines.append("How you describe yourself and how you answered the scenario questions point in "
                     "different directions. You may want to review your answers before continuing.")
    return lines
