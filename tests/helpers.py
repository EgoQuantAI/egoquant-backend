"""Shared test helpers: complete questionnaire answers for a chosen tolerance score."""

from datetime import datetime, timezone

from egoquant.risk_profile import Answers, load_all

BANK, CONFIG = load_all()
GL_IDS = [f"GL{i}" for i in range(1, 14)]
ASSESSED_AT = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)   # fixed: tests never read the clock


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def gl_option(qid: str, which: str) -> str:
    """Option id with the lowest or highest points for one Grable & Lytton item."""
    options = BANK.get(qid).options
    pick = min if which == "min" else max
    return pick(options, key=lambda o: o.points).id


def gl_choices_for_score(target: int) -> dict[str, str]:
    """Answers to GL1-GL13 that sum to exactly `target` (start at the minimum, raise items in order)."""
    choices = {qid: gl_option(qid, "min") for qid in GL_IDS}
    remaining = target - 13
    for qid in GL_IDS:
        options = sorted(BANK.get(qid).options, key=lambda o: o.points)
        for opt in reversed(options):
            if opt.points - 1 <= remaining:
                choices[qid] = opt.id
                remaining -= opt.points - 1
                break
    assert remaining == 0, f"cannot build score {target}"
    return choices


def make_answers(gl_score: int = 30, *, invested: float = 50_000, needed: float = 0,
                 gl_overrides: dict[str, str] | None = None, **choices) -> Answers:
    """A complete, unrestricted questionnaire (capacity 5, full knowledge) unless overridden."""
    base = {
        **gl_choices_for_score(gl_score),
        "B1": "growth", "B2": "gt5", "B4": "gt6", "B5": "lt25", "B6": "stable",
        "K1": "more", "K2": "less", "K3": "false", "K5": "y3_5",
        "D1": "yes",
    }
    base.update(gl_overrides or {})
    multi = {"K4": choices.pop("K4", ["stocks"])}
    base.update(choices)
    return Answers(questionnaire_version=BANK.questionnaire_version, choices=base, multi_choices=multi,
                   amount_invested=invested, amount_needed=needed)
