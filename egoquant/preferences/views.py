"""The investor's preferences as small Black-Litterman views for portfolio method v2 (docs/PORTFOLIO_V2.md, section 4).

    build_views(preferences, interest_assets, behavioral_constraints, assets) -> list[PortfolioView]

Pure and deterministic: no I/O, no randomness, no LLM. The same inputs give the same list in the same order.

What a view is: "an area the investor chose earns a little more than the neutral prior" (+0.5% a year, confidence 0.30),
"an industry the investor avoids earns a little less" (-0.5%). A tilt of an assumption, never a forecast; the Risk
Engine, the class limits and Agent B's limits still decide what can be held.

The rules (the simplest reading of the brief; every number is PROVISIONAL and a keyword argument):
  * `preferences` is any of: the investor's PreferenceAnswers (the pipeline's `preferences`; only the explicit picks count,
    the industries Agent C adds for the individual-stock style do not), what egoquant.preferences.to_interest_fields
    returns ({"interest_areas": [...], "avoid_sectors": [...], ...}), or an object with those attributes (Agent C's
    InvestorInterests). None = no preference.
  * `assets` are the products the investor was offered: AssetInfo (Agent A), or Agent C's SelectedInstrument / the
    catalog's Instrument. Whatever is given, an asset's areas, industry and region are read the same way (see
    `assets_for_view`).
  * Each chosen interest area is ONE view. It is a "sector" view for the ten industries, otherwise an "area" view, or a
    "region" view for the market areas of question P3 when only the region label is on the assets. The first
    kind that matches an offered asset is used.
  * Each avoided industry is one "sector" view of -0.5%, same confidence.
  * Nothing for products the investor was not offered: a view needs at least one asset in `assets` that it applies to.
  * Interest assets (Agent C's picks for the investor's interests) that no view above applies to get one "asset" view
    each, so the tilt still reaches them when nothing names their area. Sorted by ticker.
  * Agent B only tightens: a hard behavioral limit never creates or raises a view. It can only REMOVE a positive view
    whose assets it shuts completely (class or asset capped at zero). Limits below the confidence floor are ignored,
    exactly as Agent A ignores them.
  * At most `max_views` (20) views. Avoided industries are kept first, then the chosen areas in the preference order,
    then asset views; the list that comes out is ordered chosen areas, avoided industries, assets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from egoquant.agents.market_intel.catalog import DEFAULT_CATALOG, SECTORS, Instrument, normalize_interest_area
from egoquant.models import Constraint, PortfolioView
from egoquant.preferences.form import AREA_PRIORITY

# PROVISIONAL (decided by Onur, 19 Oct): a small tilt, not a bet.
VIEW_EXCESS = 0.005          # annual return assumed above (chosen) or below (avoided) the neutral prior
VIEW_CONFIDENCE = 0.30       # confidence in each view, in (0, 1]
MAX_VIEWS = 20
MIN_BEHAVIORAL_CONFIDENCE = 0.5     # same floor as AgentConfig.min_behavioral_confidence

# The market areas of question P3 and the label Agent C puts on AssetInfo.region (egoquant.agents.market_intel.catalog.
# region_of: "US", "developed ex-US", "emerging", "global"; case and punctuation ignored). China has no region of its own.
REGION_OF_AREA: dict[str, str] = {"us_stocks": "us", "international_stocks": "developed_ex_us",
                                  "emerging_markets": "emerging"}
_RANK = {a: i for i, a in enumerate(AREA_PRIORITY)}


def normalize_key(text: str | None) -> str:
    """"Developed ex-US" and "developed_ex_us" are the same label."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Instrument]:
    return {i.ticker: i for i in DEFAULT_CATALOG}


@dataclass(frozen=True)
class _Facts:
    """What matters about one offered product, whatever object it came as."""

    asset: Any
    asset_id: str
    asset_class: str | None
    areas: frozenset[str]
    sector: str
    region: str


def _facts(asset: Any) -> _Facts:
    """AssetInfo (asset_id, area, sector, region), Instrument (ticker, areas, sector) or SelectedInstrument (ticker).

    The areas of a product are every label written in `area` (one string; several may be separated by , ; |), the
    list in `areas`, and, for a product of the catalog, ALL the areas the catalog gives it: Agent C writes only the
    first area into AssetInfo.area, but an investor who chose "us_stocks" means VTI whose first area is "stocks"."""
    ticker = getattr(asset, "asset_id", None) or getattr(asset, "ticker", None)
    if not ticker:
        raise TypeError(f"{type(asset).__name__} has neither asset_id nor ticker")
    known = _catalog().get(ticker)
    labels = set(re.split(r"[,;|]", getattr(asset, "area", None) or ""))
    labels |= set(getattr(asset, "areas", None) or [])
    labels |= set(known.areas) if known is not None else set()
    sector = getattr(asset, "sector", None) or (known.sector if known is not None else None)
    return _Facts(asset=asset, asset_id=ticker, asset_class=getattr(asset, "asset_class", None),
                  areas=frozenset(k for k in map(normalize_key, labels) if k), sector=normalize_key(sector),
                  region=normalize_key(getattr(asset, "region", None)))


def _matching(kind: str, key: str, facts: Iterable[_Facts]) -> list[_Facts]:
    wanted = normalize_key(key)
    if kind == "asset":
        return [f for f in facts if f.asset_id == key]
    if kind == "area":
        return [f for f in facts if wanted in f.areas]
    if kind == "sector":
        return [f for f in facts if f.sector == wanted]
    if kind == "region":
        return [f for f in facts if f.region == wanted]
    raise ValueError(f"unknown view kind {kind!r}")


def assets_for_view(kind: str, key: str, assets: Iterable[Any]) -> list[Any]:
    """The assets a view applies to, in the order given. The single matching rule of portfolio method v2:

        area   -> one of the product's areas is the key (AssetInfo.area, Instrument.areas, and the catalog's full list)
        sector -> the product's industry is the key (AssetInfo.sector, or the catalog's)
        region -> AssetInfo.region is the key
        asset  -> the ticker is the key (exact)
    Labels are compared with case and punctuation ignored (except tickers).
    """
    return [f.asset for f in _matching(kind, key, [_facts(a) for a in assets])]


def _interest_fields(preferences: Any, bank: Any = None) -> tuple[list[str], list[str]]:
    """(interest areas, avoided industries) from any of the accepted forms of `preferences`."""
    if preferences is None:
        return [], []
    if hasattr(preferences, "choices") and hasattr(preferences, "preferences_version"):       # PreferenceAnswers
        if bank is None:
            from egoquant.preferences import load_preference_bank
            bank = load_preference_bank()
        areas: list[str] = []
        avoid: list[str] = []
        for question in bank.questions:
            for option_id in preferences.choices.get(question.id, []):
                option = question.option(option_id)
                areas += option.areas
                if option.sector:
                    avoid.append(option.sector)
        return areas, avoid
    if isinstance(preferences, Mapping):
        return ([str(v) for v in preferences.get("interest_areas") or []],
                [str(v) for v in preferences.get("avoid_sectors") or []])
    if hasattr(preferences, "interest_areas") or hasattr(preferences, "avoid_sectors"):
        return ([str(v) for v in getattr(preferences, "interest_areas", None) or []],
                [str(v) for v in getattr(preferences, "avoid_sectors", None) or []])
    raise TypeError("preferences must be the investor's PreferenceAnswers, the output of to_interest_fields (a dict with "
                    f"interest_areas and avoid_sectors) or an object with those attributes, not {type(preferences).__name__}")


def _first_match(candidates: Sequence[tuple[str, str]], facts: Sequence[_Facts]) -> tuple[str, str] | None:
    for kind, key in candidates:
        if _matching(kind, key, facts):
            return kind, key
    return None


def _area_candidates(area: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if area in SECTORS:
        out.append(("sector", area))
    out.append(("area", area))
    if area in REGION_OF_AREA:
        out.append(("region", REGION_OF_AREA[area]))
    return out


def _shut(constraints: Sequence[Constraint] | None, floor: float) -> tuple[set[str], set[str]]:
    """(asset classes, asset ids) that a hard behavioral limit at or below zero rules out completely."""
    classes: set[str] = set()
    ids: set[str] = set()
    for c in constraints or []:
        if c.source != "behavioral" or not c.hard or c.value > 0:
            continue
        if c.confidence is not None and c.confidence < floor:
            continue
        if c.type == "max_class_weight" and c.asset_class:
            classes.add(c.asset_class)
        elif c.type == "max_equity":
            classes.add("equity")
        elif c.type == "max_asset_weight" and c.asset_id:
            ids.add(c.asset_id)
    return classes, ids


def build_views_with_notes(preferences: Any, interest_assets: Sequence[str] | None,
                           behavioral_constraints: Sequence[Constraint] | None, assets: Sequence[Any], *,
                           excess: float = VIEW_EXCESS, confidence: float = VIEW_CONFIDENCE,
                           max_views: int = MAX_VIEWS,
                           min_behavioral_confidence: float = MIN_BEHAVIORAL_CONFIDENCE,
                           bank: Any = None) -> tuple[list[PortfolioView], list[str]]:
    """The views and, for everything that was left out, one plain sentence why (for logs and the comparison table)."""
    facts = [_facts(a) for a in assets]
    notes: list[str] = []
    shut_classes, shut_ids = _shut(behavioral_constraints, min_behavioral_confidence)
    chosen_raw, avoid_raw = _interest_fields(preferences, bank)

    def is_shut(f: _Facts) -> bool:
        return f.asset_class in shut_classes or f.asset_id in shut_ids

    def view(kind: str, key: str, sign: float) -> PortfolioView:
        return PortfolioView(kind=kind, key=key, annual_excess=sign * excess, confidence=confidence)

    # --- avoided industries -------------------------------------------------------------------------------------
    asked_to_avoid = set(avoid_raw)
    for unknown in sorted(asked_to_avoid - set(SECTORS)):
        notes.append(f"avoided industry {unknown!r} is not a known industry, so no view")
    avoided = [a for a in SECTORS if a in asked_to_avoid]
    avoid_views: list[PortfolioView] = []
    for sector in avoided:
        found = _first_match([("sector", sector), ("area", sector)], facts)
        if found is None:
            notes.append(f"avoided industry '{sector}': no offered product is in it, so no view")
            continue
        avoid_views.append(view(*found, -1.0))

    # --- chosen interest areas, in the preference order -----------------------------------------------------------
    chosen: list[str] = []
    for raw in chosen_raw:
        try:
            area = normalize_interest_area(raw)
        except ValueError:
            notes.append(f"interest area {raw!r} is not a known area, so no view")
            continue
        if area not in chosen:
            chosen.append(area)
    chosen.sort(key=lambda a: (_RANK.get(a, len(_RANK)), a))
    positive: list[PortfolioView] = []
    seen: set[tuple[str, str]] = set()
    for area in chosen:
        if area in avoided:
            notes.append(f"'{area}' is both chosen and avoided; the avoided industry wins")
            continue
        found = _first_match(_area_candidates(area), facts)
        if found is None:
            notes.append(f"interest area '{area}': no offered product is labelled with it, so no area view "
                         "(its interest assets may still get an asset view)")
            continue
        matched = _matching(*found, facts)
        if all(is_shut(f) for f in matched):
            notes.append(f"interest area '{area}': Agent B's limits rule out all of its products, so no view")
            continue
        if found in seen:
            continue                       # two areas that resolve to the same label are one view
        seen.add(found)
        positive.append(view(*found, +1.0))

    # --- interest assets that no view reaches -------------------------------------------------------------------
    covered = {f.asset_id for v in [*positive, *avoid_views] for f in _matching(v.kind, v.key, facts)}
    offered = {f.asset_id: f for f in facts}
    asset_views: list[PortfolioView] = []
    for ticker in sorted(dict.fromkeys(interest_assets or [])):
        found_asset = offered.get(ticker)
        if found_asset is None or ticker in covered or is_shut(found_asset) or len(ticker) > 40:
            continue
        asset_views.append(view("asset", ticker, +1.0))

    # --- the cap: avoided industries first, then chosen areas, then assets ------------------------------------------
    budget = max(0, max_views)
    kept_avoid = avoid_views[:budget]
    budget -= len(kept_avoid)
    kept_positive = positive[:budget]
    budget -= len(kept_positive)
    kept_assets = asset_views[:budget]
    dropped = (len(avoid_views) - len(kept_avoid)) + (len(positive) - len(kept_positive)) \
        + (len(asset_views) - len(kept_assets))
    if dropped:
        notes.append(f"{dropped} view(s) left out because at most {max_views} are used")
    return [*kept_positive, *kept_avoid, *kept_assets], notes


def build_views(preferences: Any, interest_assets: Sequence[str] | None,
                behavioral_constraints: Sequence[Constraint] | None, assets: Sequence[Any], **options: Any,
                ) -> list[PortfolioView]:
    """The investor's views for portfolio method v2; see the module docstring. `options` are the PROVISIONAL numbers
    (excess, confidence, max_views, min_behavioral_confidence) and `bank` (the preference question bank, only read
    when `preferences` are PreferenceAnswers; default: the shipped one)."""
    return build_views_with_notes(preferences, interest_assets, behavioral_constraints, assets, **options)[0]


__all__ = ["MAX_VIEWS", "VIEW_CONFIDENCE", "VIEW_EXCESS", "assets_for_view", "build_views", "build_views_with_notes",
           "normalize_key"]
