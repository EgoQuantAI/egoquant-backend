"""Regime-aware block sampler for the goal Monte Carlo (numpy only, vectorised, seeded).

The existing goal simulation (agents/quant/goal.py) builds each simulated month by drawing ONE block of 21 consecutive
trading days uniformly from all of history. Every block is equally likely to come next, whatever came before, so a
sustained crisis (2008-09 lasted about nine months) is broken up into isolated bad months and goal chances come out
too optimistic.

This sampler keeps the same blocks but chooses them in a regime-aware way:
  1. Every historical block is labelled with a market regime (calm / stressed / crisis) from the fitted HMM
     (label_blocks: the regime of the block's FIRST day; see its docstring for why).
  2. Each simulated path follows its own regime chain: the first month's regime is drawn from today's regime
     probabilities, and each later month's from the row of P^21 (the regime chances 21 trading days later) for the
     previous month's regime.
  3. Each month's block is drawn uniformly from the historical blocks labelled with that month's regime.
So crises now last as long, and recur as often, as the learned transition matrix says, and calm stretches do too.

Vectorised: the chains are pre-generated as an int8 array (n_paths, n_blocks) with one numpy step per month (no
Python loop over paths), then block indices are drawn per regime with boolean masks, in chunks of paths so the
temporary arrays stay small. The main draws have the same shape and dtype (int64) as the existing sampler's, so the
rest of the simulation is unchanged. The levers' extra months are int32 (the same values; goal.goal_levers stacks them
onto the int64 draws, so they are never indexed as int32): that halves the one array the regime path holds through
the simulation's memory peak. Given `relabel` (as the goal simulation does), the sampler also drops its regime pools
after the main draws and rebuilds them when the levers or the history-half checks need them, so it holds nothing
large through the peak (measured: below the block bootstrap's at 15 years, a few KiB above it, the sampler's own
record, at 30 and 60). Same inputs and seed, same draws.
"""

from __future__ import annotations

import numpy as np

from egoquant.regime.model import RegimeModel

LONG_CRISIS_BLOCKS = 3           # 3 consecutive 21-day blocks = 63 trading days, about 3 months
FILL_CHUNK = 1000                # paths filled per step (bounds the temporary arrays)


def label_blocks(block_start_dates, model: RegimeModel) -> np.ndarray:
    """The regime of each historical block = the regime of its FIRST trading day (int8; -1 = unlabelled).

    Why the first day and not the majority over the block: in the simulation, month k's regime is the state the chain
    is in when the month STARTS, and month k+1's regime is drawn from P^21, the chance of each regime 21 trading days
    later. Labelling a block by its first day is exactly that definition, so the labelled blocks and the chain
    describe the same thing, and a calm-starting month that turned into a crash stays in the calm pool as often as
    it really happened (that is how a crisis can begin). A majority label would describe a different quantity (the
    regime most of the month) that P^21 does not model.
    """
    return model.labels_on(block_start_dates)


def regime_pools(block_labels: np.ndarray, n_states: int = 3) -> list[np.ndarray]:
    """For each regime, the indices of the historical blocks labelled with it."""
    return [np.flatnonzero(block_labels == r) for r in range(n_states)]


class RegimeSampler:
    """Draws block indices for the goal simulation from regime chains (see the module docstring)."""

    def __init__(self, pools: list[np.ndarray], start_probs: np.ndarray, block_transition: np.ndarray,
                 relabel=None):
        """relabel: optional callable returning the historical blocks' regime labels (label_blocks). With it, draws()
        frees the pools and the paths' last regimes; both are rebuilt from the labels when needed (a path's last
        regime is the label of its last drawn block, since every block comes from its own regime's pool)."""
        self.pools = [np.asarray(p, dtype=np.int32) for p in pools]       # int32: half the memory, same draws
        self._relabel = relabel
        self._main: np.ndarray | None = None                # the main draws (a reference, not a copy), with relabel
        self.start_probs = np.asarray(start_probs, dtype=float)
        self.block_transition = np.asarray(block_transition, dtype=float)
        # thresholds for inverse-CDF sampling: state = number of cumulative thresholds below the uniform draw
        self._start_cut = np.cumsum(self.start_probs)[:-1]
        self._row_cut = np.cumsum(self.block_transition, axis=1)[:, :-1]
        self.n_states = len(self.pools)
        self.last_states: np.ndarray | None = None          # each path's final regime (to extend paths later)
        self.stats: dict = {}

    # ------------------------------------------------------------------ chains
    def chains(self, n_paths: int, n_blocks: int, rng: np.random.Generator,
               start_states: np.ndarray | None = None) -> np.ndarray:
        """Regime of every simulated month: int8 (n_paths, n_blocks). Month 0 is drawn from start_probs, or, when
        start_states is given (extending existing paths), from P^21's row for each path's last regime."""
        out = np.empty((n_blocks, n_paths), dtype=np.int8)       # one contiguous row per month, returned transposed
        cuts = [self._row_cut[:, j].copy() for j in range(self.n_states - 1)]
        prev = None if start_states is None else np.asarray(start_states, dtype=np.int8)
        for m in range(n_blocks):
            u = rng.random(n_paths)
            row = out[m]
            for j in range(self.n_states - 1):                   # inverse CDF: count the thresholds below u
                cut = self._start_cut[j] if prev is None else cuts[j][prev]
                if j == 0:
                    np.copyto(row, (u >= cut).view(np.int8))
                else:
                    row += (u >= cut).view(np.int8)
            prev = row
        return out.T

    def fill(self, chains: np.ndarray, rng: np.random.Generator, pools: list[np.ndarray] | None = None,
             dtype=np.int64) -> np.ndarray:
        """Block index of every simulated month: uniform within the pool of that month's regime. int64 by default,
        like the existing sampler's draws."""
        pools = self.current_pools() if pools is None else pools      # rebuilt if draws() freed them
        draws = np.empty(chains.shape, dtype=dtype)
        for i in range(0, chains.shape[0], FILL_CHUNK):
            c = np.ascontiguousarray(chains[i:i + FILL_CHUNK])
            d = draws[i:i + FILL_CHUNK]                          # whole rows: a contiguous slice of the draws
            for r in range(len(pools)):
                mask = c == r
                k = int(np.count_nonzero(mask))
                if k:
                    d[mask] = pools[r][rng.integers(0, len(pools[r]), size=k)]
        return draws

    # ------------------------------------------------------------------ what the goal simulation calls
    def draws(self, n_paths: int, months: int, seed: int) -> np.ndarray:
        """The main draws (same seeded Generator pattern as the existing sampler: default_rng(seed)). Also keeps
        each path's last regime (for longer horizons) and the chain statistics shown in the report."""
        rng = np.random.default_rng(seed)
        chains = self.chains(n_paths, months, rng)
        draws = self.fill(chains, rng)
        self.stats = chain_stats(chains, self.n_states)
        if self._relabel is None:
            self.last_states = chains[:, -1].copy()
        else:                                               # rebuilt on demand (current_pools, _last_states)
            self.pools, self._main = None, draws
        del chains
        return draws

    @property
    def extendable(self) -> bool:
        return self.last_states is not None or self._main is not None

    def current_pools(self) -> list[np.ndarray]:
        if self.pools is not None:
            return self.pools
        return [np.asarray(p, dtype=np.int32) for p in regime_pools(self._relabel(), self.n_states)]

    def _last_states(self) -> np.ndarray:
        if self.last_states is not None:
            return self.last_states
        return self._relabel()[self._main[:, -1]]

    def extend(self, extra_months: int, seed: int) -> np.ndarray:
        """Further months for every path (a later goal date), continuing each path's own regime chain. int32: the
        same values, half the memory (the caller stacks them onto the int64 main draws)."""
        if not self.extendable:
            raise RuntimeError("extend() needs draws() first")
        last = self._last_states()
        if extra_months <= 0:
            return np.empty((len(last), 0), dtype=np.int32)
        rng = np.random.default_rng(seed)
        chains = self.chains(len(last), extra_months, rng, start_states=last)
        return self.fill(chains, rng, self.current_pools(), dtype=np.int32)

    def range_draws(self, lo: int, hi: int, n_paths: int, months: int, rng: np.random.Generator,
                    min_pool: int) -> np.ndarray | None:
        """Draws using only blocks lo..hi-1 (the older-half / newer-half check). None if a regime has fewer than
        min_pool blocks in that range; the caller then draws that half uniformly, as before."""
        pools = [p[(p >= lo) & (p < hi)] for p in self.current_pools()]
        if min(len(p) for p in pools) < min_pool:
            return None
        chains = self.chains(n_paths, months, rng)
        return self.fill(chains, rng, pools)


def chain_stats(chains: np.ndarray, n_states: int = 3) -> dict:
    """Facts computed from the simulated regime chains themselves (not estimated):
    long_crisis_share: share of paths with at least one crisis (the most volatile regime) lasting LONG_CRISIS_BLOCKS
    blocks (~3 months) or more; month_shares: share of simulated months in each regime."""
    n_paths, n_blocks = chains.shape
    crisis = chains == n_states - 1
    if n_blocks >= LONG_CRISIS_BLOCKS:
        run = crisis[:, : n_blocks - LONG_CRISIS_BLOCKS + 1].copy()
        for k in range(1, LONG_CRISIS_BLOCKS):
            run &= crisis[:, k: n_blocks - LONG_CRISIS_BLOCKS + 1 + k]
        long_share = float(run.any(axis=1).mean())
    else:
        long_share = 0.0
    counts = [int(np.count_nonzero(chains == r)) for r in range(n_states)]      # no int64 copy of the chains
    return {"long_crisis_share": long_share, "month_shares": [c / chains.size for c in counts],
            "long_crisis_blocks": LONG_CRISIS_BLOCKS}
