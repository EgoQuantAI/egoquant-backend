"""Agent A: the Quantitative Portfolio Engine.

    agent.py     QuantAgent: Agent C's data -> candidate menu -> Agent B's limits -> final proposal
    core.py      pure math (covariance, risk metrics, stress tests, the constrained optimizer)
    backtest.py  walk-forward backtest harness and the point-in-time HistoricalMarketProvider
"""

from egoquant.agents.quant.agent import DataValidationError, MarketModel, QuantAgent
from egoquant.scenarios import DEFAULT_STRESS_SCENARIOS

__all__ = ["DEFAULT_STRESS_SCENARIOS", "DataValidationError", "MarketModel", "QuantAgent"]
