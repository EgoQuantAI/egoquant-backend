"""The safe switch between the existing block bootstrap and the regime-aware sampler.

    decide(dates, block_days, as_of, data_source)  ->  RegimeDecision | None

None means "flag off" (EGOQUANT_REGIME_MC=0, the kill switch): the caller runs the existing code path untouched
(byte-for-byte the old result, no new fields).
Otherwise the decision either holds a working RegimeSampler or a fallback reason, and `record()` says which sampler
ran. The regime code can never make a request fail: every problem, including an unexpected exception, becomes a
fallback to the block bootstrap with a short reason label (logged without any personal data).

Fallback reasons (the existing sampler runs, `sampler: "block_bootstrap"`, `fallback_reason` set):
    artifact_missing | artifact_invalid | artifact_hash_mismatch   the model file cannot be trusted
    synthetic_data                                                 invented prices have no real market regimes
    short_overlap        the simulation's history overlaps the labelled dates by less than MIN_OVERLAP_DAYS (~5 years)
    thin_regime_pool     some regime has fewer than MIN_POOL_BLOCKS eligible historical blocks
    model_changed        (What-If) the report was simulated with a model file that is no longer the deployed one
    sampler_error        any exception inside the regime code

Feature flag: EGOQUANT_REGIME_MC (default "1" = on, the product owner's decision; see
eval/results/regime_v2_summary.md; "0", "false", "off" or "no" = off, exactly the old block bootstrap). Read on every request.

Reproducibility. With the regime sampler, the sampler name, model version, model SHA-256 and today's regime
probabilities go into input_hash (hash_part) and the engine version becomes ENGINE_VERSION + build metadata
(engine_version). The What-If Agent replays a report with the sampler the report used: the record is kept in the
sealed what_if_context, and forced_sampler() makes Agent A's re-optimisation use it too.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from datetime import date

import numpy as np

from egoquant.regime.model import (
    MARKET_PROXY,
    MODEL_PATHS,
    MODEL_VERSION,
    RegimeModelError,
    block_transition_matrix,
    current_regime_probs,
    load_regime_model,
    model_path_for,
    recent_market_returns,
    recent_model_returns,
)
from egoquant.regime.sampler import LONG_CRISIS_BLOCKS, RegimeSampler, label_blocks, regime_pools

logger = logging.getLogger(__name__)

FLAG = "EGOQUANT_REGIME_MC"
SAMPLER_BOOTSTRAP = "block_bootstrap"
SAMPLER_REGIME = MODEL_VERSION                  # "regime_hmm_v1"; a record's sampler is the model version that ran
DEFAULT_MODEL_VERSION = "regime_hmm_v2"         # the model decide() uses when no report is replayed (PROTOCOL_v2.md)
ENGINE_VERSION = "0.4.0"                        # Agent A's engine with the regime sampler (0.3.0 without it)
WHAT_IF_ENGINE_VERSION = "what-if-0.2.0"        # the What-If Agent with the regime sampler (what-if-0.1.0 without it)
MIN_OVERLAP_DAYS = 1260                         # PROVISIONAL: ~5 years, the goal simulation's own minimum history
MIN_POOL_BLOCKS = 8                             # PROVISIONAL: fewest eligible blocks any regime may have
BLOCK_LABEL_RULE = "first_day"

_forced: contextvars.ContextVar[dict | None] = contextvars.ContextVar("egoquant_regime_forced", default=None)


def flag_on() -> bool:
    return os.environ.get(FLAG, "1").strip().lower() in {"1", "true", "on", "yes"}


@contextlib.contextmanager
def forced_sampler(record: dict | None):
    """Inside this block, decide() follows `record` (a report's sampler record) instead of the flag. A record that
    is not the regime sampler means the plain block bootstrap. Used by the What-If Agent's re-optimisation."""
    token = _forced.set(record if record is not None else {"sampler": SAMPLER_BOOTSTRAP})
    try:
        yield
    finally:
        _forced.reset(token)


class RegimeDecision:
    """What goal.analyze_goal and the What-If Agent use. `sampler` is None after a fallback."""

    def __init__(self, sampler: RegimeSampler | None, record: dict):
        self.sampler = sampler
        self._record = record

    @property
    def active(self) -> bool:
        return self.sampler is not None

    def _fail(self, where: str) -> None:
        """An exception inside the regime code: the block bootstrap from here on, and the record says so. If the main
        draws had already succeeded (the chances came from the regime sampler), the record keeps saying that and adds
        which later part (lever horizons, history-half checks) used the block bootstrap instead."""
        logger.warning("regime sampler failed in %s; using the block bootstrap", where, exc_info=True)
        self.sampler = None
        if where == "draws" or not is_regime(self._record):
            self._record = fallback_record("sampler_error", self._record)
        else:
            self._record = {**self._record, "fallback_reason": f"sampler_error_in_{where}"}

    def draws(self, n_blocks: int, n_paths: int, months: int, seed: int) -> np.ndarray | None:
        """Regime-aware draws, or None (the caller then draws exactly as before)."""
        if self.sampler is None:
            return None
        try:
            if n_blocks != self._record["n_blocks"]:
                raise ValueError("block count changed")
            out = self.sampler.draws(n_paths, months, seed)
            s = self.sampler.stats
            self._record = {**self._record, "long_crisis_share": s["long_crisis_share"],
                            "long_crisis_in_100": in_100(s["long_crisis_share"]),
                            "month_shares": s["month_shares"], "long_crisis_blocks": s["long_crisis_blocks"]}
            return out
        except Exception:
            self._fail("draws")
            return None

    def extend(self, extra_months: int, seed: int) -> np.ndarray | None:
        """Further months continuing each path's regime chain, or None (the caller extends as before)."""
        if self.sampler is None or not self.sampler.extendable:
            return None
        try:
            return self.sampler.extend(extra_months, seed)
        except Exception:
            self._fail("extend")
            return None

    def range_draws(self, lo: int, hi: int, n_paths: int, months: int, rng: np.random.Generator) -> np.ndarray | None:
        if self.sampler is None:
            return None
        try:
            out = self.sampler.range_draws(lo, hi, n_paths, months, rng, MIN_POOL_BLOCKS)
        except Exception:
            self._fail("range_draws")
            return None
        used = self._record["sampler"] if out is not None else f"{SAMPLER_BOOTSTRAP} (thin_regime_pool in this half)"
        self._record = {**self._record, "period_check_samplers": [*self._record.get("period_check_samplers", []), used]}
        return out

    def record(self) -> dict:
        return dict(self._record)


def fallback_record(reason: str, previous: dict | None = None) -> dict:
    out = {"sampler": SAMPLER_BOOTSTRAP, "fallback_reason": reason, "model_version": MODEL_VERSION}
    if previous and previous.get("model_sha256"):
        out["model_sha256"] = previous["model_sha256"]
    return out


def decide(dates, block_days: int, as_of: date, data_source: str | None = None,
           forced: dict | None = None, model_path=None) -> RegimeDecision | None:
    """The sampler for one goal simulation whose history has trading days `dates` (the simulation's daily index).

    forced: a report's sampler record to replay (What-If); otherwise the record set by forced_sampler(), otherwise
    the feature flag. Returns None for the plain block bootstrap without any record (flag off, or a replayed report
    that used the block bootstrap). Never raises.
    """
    forced = forced if forced is not None else _forced.get()
    if forced is None and not flag_on():
        return None
    if forced is not None and not is_regime(forced):
        return None
    try:
        return _decide(dates, block_days, as_of, data_source, forced, model_path)
    except Exception:
        logger.warning("regime sampler unavailable (sampler_error); using the block bootstrap", exc_info=True)
        return RegimeDecision(None, fallback_record("sampler_error"))


def _decide(dates, block_days, as_of, data_source, forced, model_path) -> RegimeDecision:
    def fallback(reason: str, model=None) -> RegimeDecision:
        logger.warning("regime sampler not used (%s); using the block bootstrap", reason)
        rec = fallback_record(reason)
        if model is not None:
            rec["model_sha256"] = model.sha256
        return RegimeDecision(None, rec)

    if model_path is None:
        # a replayed report loads the model it was simulated with (by version); otherwise the default model
        version = forced.get("model_version") if forced is not None else DEFAULT_MODEL_VERSION
        model_path = model_path_for(version) or model_path_for(DEFAULT_MODEL_VERSION)
    try:
        model = load_regime_model(model_path)
    except RegimeModelError as err:
        return fallback(err.reason)
    if forced is not None and forced.get("model_sha256") != model.sha256:
        return fallback("model_changed", model)
    if data_source == "synthetic":
        return fallback("synthetic_data", model)

    day_index = np.asarray(dates, dtype="datetime64[D]")
    n_blocks = len(day_index) - block_days + 1
    if n_blocks < 1:
        return fallback("short_overlap", model)
    labels_days = model.labels_on(day_index)
    overlap = int(np.count_nonzero(labels_days >= 0))
    if overlap < MIN_OVERLAP_DAYS:
        return fallback("short_overlap", model)
    block_labels = label_blocks(day_index[:n_blocks], model)
    pools = regime_pools(block_labels, model.n_states)
    sizes = [len(p) for p in pools]
    if min(sizes) < MIN_POOL_BLOCKS:
        return fallback("thin_regime_pool", model)

    if forced is not None:
        start = np.asarray(forced["start_probs"], dtype=float)
        if start.shape != (model.n_states,) or not np.all(np.isfinite(start)) or np.any(start < 0) or abs(start.sum() - 1) > 1e-9:
            return fallback("sampler_error", model)
        source, data_end = forced.get("start_source", "report"), forced.get("regime_data_end")
    else:
        recent = recent_market_returns(as_of) if len(model.tickers) == 1 else recent_model_returns(as_of, model)
        if recent is None:
            start, source = current_regime_probs(None, model)
            data_end = None
        else:
            returns, last_day, gap = recent
            start, source = current_regime_probs(returns, model, steps_ahead=gap)
            data_end = str(last_day) if source == "filtered" else None
    p_block = block_transition_matrix(model.transition, block_days)
    # the sampler frees its pools after the main draws and relabels from the model (cached) and the simulation's own
    # date index (already in memory) when the levers or the half checks need them: same pools, nothing held meanwhile
    sampler = RegimeSampler(pools, start, p_block,
                            relabel=lambda: label_blocks(np.asarray(dates, dtype="datetime64[D]")[:n_blocks], model))
    today = int(np.argmax(start))
    record = {
        "sampler": model.version, "model_version": model.version, "model_sha256": model.sha256,
        "fit_start": str(model.fit_start), "fit_end": str(model.fit_end),
        "start_probs": [float(v) for v in start], "start_source": source, "regime_data_end": data_end,
        "regime_today": model.state_names[today], "regime_today_probability": float(start[today]),
        "state_names": list(model.state_names), "block_label": BLOCK_LABEL_RULE, "block_days": int(block_days),
        "n_blocks": int(n_blocks), "pool_blocks": sizes, "unlabelled_blocks": int(np.count_nonzero(block_labels < 0)),
        "labelled_days": overlap, "long_crisis_blocks": LONG_CRISIS_BLOCKS, "replayed_from_report": forced is not None,
    }
    if model.tickers != (MARKET_PROXY,):
        record["model_funds"] = list(model.tickers)
    return RegimeDecision(sampler, record)


# --------------------------------------------------------------------------------------------------------------
# Reproducibility helpers
# --------------------------------------------------------------------------------------------------------------
def is_regime(record: dict | None) -> bool:
    """True when the regime sampler ran (the record's sampler is a known regime model version)."""
    return bool(record) and record.get("sampler") in MODEL_PATHS


def hash_part(record: dict | None) -> str:
    """What goes into input_hash: the sampler name, plus (regime) model version, SHA-256 and start probabilities."""
    if not record:
        return ""
    if not is_regime(record):
        return f"sampler:{SAMPLER_BOOTSTRAP}:{record.get('fallback_reason') or ''}"
    return "sampler:" + json.dumps([record["sampler"], record["model_version"], record["model_sha256"],
                                    record["start_probs"]], separators=(",", ":"))


def engine_version(record: dict | None, base: str, regime_version: str = ENGINE_VERSION) -> str:
    """The engine version for a result: unchanged without the regime sampler; with it, the bumped version plus
    build metadata naming the sampler and the first 12 characters of the model's SHA-256 (semver "+" metadata)."""
    if not is_regime(record):
        return base
    return f"{regime_version}+{record['model_version']}.{record['model_sha256'][:12]}"


def in_100(p: float) -> int:
    """A share as "X in 100", never a misleading 0 or 100 unless exact (the report's own rule)."""
    if p <= 0:
        return 0
    if p >= 1:
        return 100
    return min(max(int(round(p * 100)), 1), 99)
