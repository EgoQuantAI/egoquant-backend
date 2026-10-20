"""EgoQuant: a deterministic wealth-advisory engine.

    risk_profile/         formal questionnaire -> C1..C5 and suitability limits
    preferences/          what the investor wants to invest in (only options their profile allows)
    agents/market_intel/  Agent C: approved universe and real market data (Tiingo, Binance)
    agents/quant/         Agent A: candidate menu and constrained portfolio proposal
    agents/behavioral/    Agent B: behavioral limits from trading history and the risk game
    risk_engine.py        independent re-check of the final proposal (final say)
    report.py             client report (structured data + one HTML page)
    pipeline.py           run_pipeline: the whole chain for one investor
    demo/                 trader personas and SYNTHETIC data for demos and tests
"""

__version__ = "0.3.0"
