"""Portfolio method v2: the flat diagnostics that travel with the chosen portfolio (PortfolioProposal.diagnostics holds
float or str only). docs/PORTFOLIO_V2.md section 4. Keys are prefixed v2_."""

from __future__ import annotations

import json

import numpy as np

TOP_RISK_CONTRIBUTIONS = 10


def risk_contributions(w: np.ndarray, cov: np.ndarray) -> np.ndarray:
    var = float(w @ cov @ w)
    return (w * (cov @ w)) / var if var > 0 else np.zeros_like(w)


def describe(model, weights: np.ndarray, binding: list[str] | None = None, seed_hex: str | None = None,
             resample_n: int = 0) -> dict[str, float | str]:
    state = getattr(model, "v2", None)
    en = 1.0 / float(np.sum(np.square(weights))) if np.sum(np.square(weights)) > 0 else 0.0
    rc = risk_contributions(weights, model.cov_annual)
    top = sorted(((model.asset_ids[i], float(rc[i])) for i in range(len(rc)) if weights[i] > 1e-6), key=lambda t: -t[1])
    out: dict[str, float | str] = {
        "v2_effective_n": en,
        "v2_largest": float(np.max(weights)),
        "v2_n_holdings": float(int(np.sum(weights > 0.005))),  # same 0.5% threshold as the report
        "v2_n_universe": float(state.n_universe if state else len(model.asset_ids)),
        "v2_n_representatives": float(len(model.asset_ids)),
        "v2_group_cap_binding": float(any(str(b).startswith("v2:group") for b in (binding or []))),
        "v2_resample_n": float(resample_n),
        "v2_risk_contribution": json.dumps({k: round(v, 4) for k, v in top[:TOP_RISK_CONTRIBUTIONS]}, sort_keys=True),
    }
    if seed_hex:
        out["v2_seed"] = seed_hex
    return out
