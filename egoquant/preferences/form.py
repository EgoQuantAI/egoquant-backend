"""Investment preferences: what this investor may choose from, and their choices as Agent C input.

Deterministic, no I/O. Every option is checked against the same rules Agent C applies, so the
form never offers something the investor would then be refused:

    an option is AVAILABLE when at least one catalog instrument behind it
      * is rated at or below the investor's max R-level,
      * is not a product type blocked by their knowledge score,
      * belongs to an asset class their C-level allows (commodity / crypto caps above 0), and
      * can be bought on one of their trading platforms (when platforms are known).

Answers are re-validated against the form (an option that was not offered is rejected), then turned
into Agent C's InvestorInterests fields. interest_areas comes out in PRIORITY order, so that if the
investor picks more than Agent C's universe budget can hold, the most common choices are served first.
"""

from __future__ import annotations

from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, SECTORS, Instrument
from egoquant.agents.market_intel.platforms import PLATFORMS, normalize_platform
from egoquant.preferences.models import (
    FormOption,
    FormQuestion,
    PreferenceAnswers,
    PreferenceBank,
    PreferenceForm,
    PreferenceQuestion,
)
from egoquant.risk_profile.models import RiskProfile, RiskProfileConfig

EXCLUSIVE_OPTIONS = {"no_preference", "none"}

# Priority when the universe budget is tight: the building blocks of a portfolio first, then the
# investor's own focus (industries, markets, bond types), then the more specialised asset classes.
AREA_PRIORITY: list[str] = [
    "stocks", "bonds", "cash",
    *SECTORS,
    "us_stocks", "international_stocks", "dividend_stocks", "emerging_markets", "small_caps", "china",
    "government_bonds", "corporate_bonds", "inflation_protected", "international_bonds", "long_term_bonds",
    "gold", "commodities", "precious_metals", "crypto", "altcoins",
]


# ==========================================================================
# Form
# ==========================================================================
_PROPER_FIRST_WORDS = ("Bitcoin", "Ether")      # option texts that start with a name keep their capital


def _in_a_sentence(text: str) -> str:
    """An option text in the middle of a sentence: "Gold" stays "gold" in "does not include gold", but
    "Bitcoin and Ether" keeps its capitals."""
    return text if text.startswith(_PROPER_FIRST_WORDS) else text[0].lower() + text[1:]


def build_form(bank: PreferenceBank, profile: RiskProfile, profile_config: RiskProfileConfig,
               trading_platforms: list[str] | None = None,
               catalog: list[Instrument] | None = None) -> PreferenceForm:
    """The preference questions with each option marked available or not (with the reason)."""
    catalog = catalog or DEFAULT_CATALOG
    rules = _Rules(profile, profile_config, trading_platforms or [])
    questions = []
    for q in bank.questions:
        options = []
        for o in q.options:
            if q.id == "P4" and o.style in ("individual", "mix"):
                reason = rules.reason([i for i in catalog if i.product_type == "stock"], "single stocks")
            elif o.areas:
                reason = rules.reason([i for i in catalog if set(o.areas) & set(i.areas)], _in_a_sentence(o.text))
            else:
                reason = None                     # "no preference", things to avoid, bond holding style
            options.append(FormOption(id=o.id, text=o.text, available=reason is None, reason=reason))
        questions.append(FormQuestion(id=q.id, type=q.type, required=q.required, text=q.text,
                                      requires=q.requires, options=options))
    return PreferenceForm(preferences_version=bank.preferences_version, c_level=profile.c_level,
                          max_r_level=profile.max_r_level, questions=questions)


class _Rules:
    """The suitability rules an option must pass (same as Agent C's universe filters)."""

    def __init__(self, profile: RiskProfile, config: RiskProfileConfig, platforms: list[str]):
        self.max_r = profile.max_r_level
        self.blocked = set(config.knowledge.complex_product_types) if profile.product_access == "blocked_complex" \
            else set()
        level = config.c_levels[profile.c_level]
        self.class_caps = {"commodity": level.max_commodity, "crypto": level.max_crypto}
        self.c_level = profile.c_level
        keys = [normalize_platform(p) for p in platforms]
        self.markets = {m for k in keys for m in PLATFORMS[k].markets}
        self.platform_names = ", ".join(PLATFORMS[k].name for k in keys)

    def allowed(self, inst: Instrument) -> bool:
        return (inst.r_level <= self.max_r and inst.product_type not in self.blocked
                and self.class_caps.get(inst.asset_class, 1.0) > 0
                and (not self.markets or inst.market in self.markets))

    def reason(self, members: list[Instrument], what: str) -> str | None:
        """None if some member is allowed; otherwise the first rule that rules them all out."""
        if not members or any(self.allowed(i) for i in members):
            return None
        # Suitability reasons first (they do not change with the platform), the platform last.
        if all(self.class_caps.get(i.asset_class, 1.0) == 0 for i in members):
            return f"your risk category {self.c_level} does not include {what}"
        low_r = min(i.r_level for i in members)
        if low_r > self.max_r:
            return (f"riskier than your category {self.c_level} allows (these products are rated risk level R{low_r} or higher; "
                    f"{self.c_level} allows up to R{self.max_r})")
        if all(i.product_type in self.blocked or i.r_level > self.max_r for i in members):
            return "needs more product knowledge (see your answers to the knowledge questions)"
        return f"cannot be bought on your trading platform(s) ({self.platform_names})"


# ==========================================================================
# Answers -> Agent C
# ==========================================================================
def _said(q) -> str:
    """The question as the person saw it, for the sentences below (never the question number)."""
    return f'"{q.text}"'


def _option_text(form: PreferenceForm, question_id: str, option_id: str) -> str:
    for q in form.questions:
        if q.id == question_id:
            return next((o.text for o in q.options if o.id == option_id), option_id)
    return option_id


def validate_answers(answers: PreferenceAnswers, form: PreferenceForm) -> None:
    """Raise ValueError unless every answer is an offered option of an applicable question."""
    if answers.preferences_version != form.preferences_version:
        raise ValueError(f"answers are for preferences {answers.preferences_version}, "
                         f"form is {form.preferences_version}")
    known = {q.id for q in form.questions}
    unknown = sorted(set(answers.choices) - known)
    if unknown:
        raise ValueError(f"answers to unknown questions: {unknown}")
    for q in form.questions:
        picked = answers.choices.get(q.id, [])
        applies = _applies(q.requires, answers)
        if picked and not applies:
            needed = " and ".join(f'"{_option_text(form, k, v)}"' for k, v in (q.requires or {}).items())
            raise ValueError(f"The question {_said(q)} only applies if you chose {needed}; please remove your answer to it")
        if applies and q.required and not picked:
            raise ValueError(f"{_said(q)} is not answered (select at least one option)")
        if q.type == "single" and len(picked) > 1:
            raise ValueError(f"{_said(q)}: choose one option")
        if len(set(picked)) != len(picked):
            raise ValueError(f"{_said(q)}: an option is selected twice")
        offered = {o.id: o for o in q.options}
        for option_id in picked:
            if option_id not in offered:
                raise ValueError(f"{_said(q)}: unknown option '{option_id}'")
            if not offered[option_id].available:
                raise ValueError(f'{_said(q)}: "{offered[option_id].text}" is not available to you: {offered[option_id].reason}')
        if set(picked) & EXCLUSIVE_OPTIONS and len(picked) > 1:
            only = (set(picked) & EXCLUSIVE_OPTIONS).pop()
            raise ValueError(f'{_said(q)}: "{_option_text(form, q.id, only)}" cannot be combined with other options')


def to_interest_fields(answers: PreferenceAnswers, form: PreferenceForm, bank: PreferenceBank) -> dict:
    """Validated answers -> InvestorInterests fields (interest_areas in priority order)."""
    validate_answers(answers, form)
    areas: list[str] = []
    style, avoid, individual_bonds = "funds", [], False
    for q in bank.questions:
        for option_id in answers.choices.get(q.id, []):
            o = q.option(option_id)
            areas += o.areas
            if o.style:
                style = o.style
            if o.sector:
                avoid.append(o.sector)
            if o.individual_bonds:
                individual_bonds = True
    rank = {a: i for i, a in enumerate(AREA_PRIORITY)}
    ordered = sorted(dict.fromkeys(areas), key=lambda a: rank.get(a, len(rank)))
    # Individual stocks with no industry picked: spread over every industry the investor does not avoid
    # (after their explicit picks, so the budget serves those first). Real estate is chosen in P1 as an
    # asset class, not in P2 as an industry, so it does not count as an industry pick here.
    if style in ("individual", "mix") and "stocks" in ordered and not set(ordered) & (set(SECTORS) - {"real_estate"}):
        ordered += [sector for sector in SECTORS if sector not in avoid and sector not in ordered]
    clash = sorted(set(ordered) & set(avoid))
    if clash:
        names = " and ".join(_option_text(form, "P5", c) for c in clash)
        raise ValueError(f"You chose {names} both as an industry you are interested in and as one to avoid; "
                         "please remove it from one of the two")
    return {"interest_areas": ordered, "instrument_style": style, "avoid_sectors": avoid,
            "wants_individual_bonds": individual_bonds}


def _applies(requires: dict[str, str] | None, answers: PreferenceAnswers) -> bool:
    return all(v in answers.choices.get(k, []) for k, v in (requires or {}).items())


__all__ = ["AREA_PRIORITY", "PreferenceQuestion", "build_form", "to_interest_fields", "validate_answers"]
