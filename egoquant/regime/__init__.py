"""Regime-aware sampling for the goal Monte Carlo (on by default; EGOQUANT_REGIME_MC=0 turns it off).

Machine learning, not an LLM: an unsupervised Gaussian Hidden Markov Model, fitted OFFLINE (scripts/fit_regime_model.py,
hmmlearn, dev only), sorts every past trading day into market regimes. The default model, regime_hmm_v2
(egoquant/data/regime_model_v2.json, eval/regime/PROTOCOL_v2.md), reads the S&P 500 fund SPY and the US bond fund AGG
and has 4 regimes (calm, normal, stressed, crisis), a number chosen by BIC; regime_hmm_v1 (3 regimes, SPY only) stays
so reports made with it replay unchanged. At runtime only the fitted JSON is loaded and everything is plain numpy:

    model.py     load and validate the artifact; HMM forward filter for today's regime; P^21
    sampler.py   regime chains per simulated path; each month's block drawn from the blocks of its regime
    hook.py      the feature flag, the fallback rules, the record of which sampler ran, input_hash/engine version

Why: the existing block bootstrap treats every past month as equally likely to come next, which breaks up sustained
crises and makes goal chances too optimistic (ZC finding Z2, bond-heavy portfolios). With the regime sampler a
crisis month is followed by the months that really tended to follow one.

Everything is HYPOTHETICAL, based on past data, and not a forecast.
"""

from egoquant.regime.hook import (
    FLAG,
    SAMPLER_BOOTSTRAP,
    SAMPLER_REGIME,
    RegimeDecision,
    decide,
    engine_version,
    flag_on,
    forced_sampler,
    hash_part,
    is_regime,
)
from egoquant.regime.model import (
    MODEL_VERSION,
    RegimeModel,
    RegimeModelError,
    block_transition_matrix,
    current_regime_probs,
    load_regime_model,
    stationary_distribution,
)
from egoquant.regime.sampler import RegimeSampler, label_blocks

__all__ = ["FLAG", "MODEL_VERSION", "SAMPLER_BOOTSTRAP", "SAMPLER_REGIME", "RegimeDecision", "RegimeModel",
           "RegimeModelError", "RegimeSampler", "block_transition_matrix", "current_regime_probs", "decide",
           "engine_version", "flag_on", "forced_sampler", "hash_part", "is_regime", "label_blocks",
           "load_regime_model", "stationary_distribution"]
