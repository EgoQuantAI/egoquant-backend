"""Portfolio method v2 (docs/PORTFOLIO_V2.md). Pure math modules; active only when portfolio_method() == "v2"."""

import os


def method_names() -> str:
    """The construction methods this build really has, in plain words (used by the report so it never names a method that did not run)."""
    from importlib.util import find_spec
    names = []
    if find_spec("egoquant.agents.quant.v2.targets") is not None:
        names += ["risk parity", "hierarchical risk parity", "maximum diversification"]
    names += ["Black-Litterman", "resampled efficiency"]
    return ", ".join(names[:-1]) + " and " + names[-1]


def portfolio_method() -> str:
    """"v2" only when EGOQUANT_PORTFOLIO_METHOD is exactly "v2" (anything else is the default engine, "v1").
    Uses egoquant.settings.portfolio_method when that exists (Damla), else the same rule here."""
    try:
        from egoquant.settings import portfolio_method as _from_settings
    except ImportError:
        return "v2" if os.environ.get("EGOQUANT_PORTFOLIO_METHOD", "").strip().lower() == "v2" else "v1"
    return _from_settings()
