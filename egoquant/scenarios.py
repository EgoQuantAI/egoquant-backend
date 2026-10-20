"""Hypothetical stress scenarios shared by Agent A, Agent B, the Risk Engine and the report.

Every number is an ASSUMPTION chosen for stress testing, not a forecast. One definition lives here so
that the optimizer, the independent Risk Engine and the client report always price the same shocks.

How a scenario hits an asset:  shock = asset_class_shocks[class]  (+ for bonds: -duration * dy +
0.5 * convexity * dy^2, with dy = rate_shock_bp / 10,000). Classes not listed are unchanged.

Commodity and crypto shocks (PROVISIONAL, from past episodes, rounded to be conservative):
  * capacity scenario: gold fell about 25-30% from peak in 2008 and about 12% in March 2020; broad
    commodities fell much more in 2008. We assume -15% for the class. Bitcoin fell about 50% in
    March 2020 and about 65% in 2022: we assume -60%.
  * commodity_crypto_selloff: a fall concentrated in real assets, like gold in 2013 (-28%) and the
    2022 crypto winter. It is the scenario that tests a portfolio's commodity and crypto sleeves.
"""

from __future__ import annotations

from egoquant.models import StressScenario

# The scenario that every loss limit (class, suitability, behavioral) refers to.
CAPACITY_SCENARIO = "equity_down_30_bond_down_5"

DEFAULT_STRESS_SCENARIOS: list[StressScenario] = [
    StressScenario(name="equity_down_20", description="Equities fall 20%, crypto falls 40%",
                   asset_class_shocks={"equity": -0.20, "crypto": -0.40}),
    StressScenario(name=CAPACITY_SCENARIO,
                   description="Equities fall 30%, bonds 5%, gold and commodities 15%, crypto 60%",
                   asset_class_shocks={"equity": -0.30, "bond": -0.05, "commodity": -0.15, "crypto": -0.60}),
    StressScenario(name="rate_up_100bp",
                   description="Parallel +100bp rate shock (bonds via duration and convexity); "
                               "gold and commodities -5%, crypto -15%",
                   asset_class_shocks={"commodity": -0.05, "crypto": -0.15}, rate_shock_bp=100.0),
    # Bonds move through the rate shock only (duration and convexity). A flat bond shock on top would
    # count the same rate rise twice (a 1-3 year Treasury fund would lose ~11%).
    StressScenario(name="inflation_shock",
                   description="Equities -10%, rates +150bp (bonds via duration and convexity), crypto -20%",
                   asset_class_shocks={"equity": -0.10, "crypto": -0.20}, rate_shock_bp=150.0),
    StressScenario(name="commodity_crypto_selloff",
                   description="Gold and commodities fall 25%, crypto falls 70%, equities fall 5%",
                   asset_class_shocks={"equity": -0.05, "commodity": -0.25, "crypto": -0.70}),
]

# Short client-facing names (the report falls back to the scenario name for anything not listed).
SCENARIO_LABELS: dict[str, str] = {
    "equity_down_20": "Stocks fall 20%",
    CAPACITY_SCENARIO: "Severe crash (stocks -30%)",
    "rate_up_100bp": "Interest rates rise 1%",
    "inflation_shock": "Inflation shock",
    "commodity_crypto_selloff": "Gold and crypto sell-off",
}
