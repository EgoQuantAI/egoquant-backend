"""Sampler correctness (egoquant/regime/sampler.py): transition frequencies, start distribution, regime pools."""

from __future__ import annotations

import numpy as np
import pytest

from egoquant.regime import block_transition_matrix, load_regime_model
from egoquant.regime.sampler import RegimeSampler, chain_stats, label_blocks, regime_pools

N_BLOCKS = 300


@pytest.fixture(scope="module")
def setup():
    m = load_regime_model()
    p21 = block_transition_matrix(m.transition, 21)
    rng = np.random.default_rng(1)
    labels = rng.choice(3, size=N_BLOCKS, p=[0.45, 0.4, 0.15]).astype(np.int8)
    labels[:5] = -1                                               # unlabelled blocks are never drawn
    start = np.array([0.2, 0.5, 0.3])
    return RegimeSampler(regime_pools(labels), start, p21), labels, p21, start


def test_empirical_transitions_match_p21(setup):
    sampler, _, p21, _ = setup
    chains = sampler.chains(20_000, 40, np.random.default_rng(7)).astype(np.int64)
    counts = np.zeros((3, 3))
    np.add.at(counts, (chains[:, :-1].ravel(), chains[:, 1:].ravel()), 1)
    freq = counts / counts.sum(axis=1, keepdims=True)
    assert np.abs(freq - p21).max() < 0.01                        # ~260k transitions per row on average


def test_start_distribution_matches_the_current_regime_probabilities(setup):
    sampler, _, _, start = setup
    first = sampler.chains(50_000, 1, np.random.default_rng(8))[:, 0]
    freq = np.bincount(first, minlength=3) / len(first)
    assert np.abs(freq - start).max() < 0.01


def test_every_block_comes_from_its_regime_pool(setup):
    sampler, labels, _, _ = setup
    rng = np.random.default_rng(9)
    chains = sampler.chains(5_000, 60, rng)
    draws = sampler.fill(chains, rng)
    assert draws.dtype == np.int64 and draws.shape == (5_000, 60)
    assert np.array_equal(labels[draws], chains)                  # the drawn block's label = the month's regime
    assert (draws >= 5).all()                                     # never an unlabelled block


def test_within_a_pool_blocks_are_uniform(setup):
    sampler, labels, _, _ = setup
    rng = np.random.default_rng(10)
    chains = np.zeros((40_000, 10), dtype=np.int8)                # all calm
    draws = sampler.fill(chains, rng)
    pool = np.flatnonzero(labels == 0)
    freq = np.bincount(draws.ravel(), minlength=N_BLOCKS)[pool] / draws.size
    assert np.abs(freq - 1 / len(pool)).max() < 0.25 / len(pool)


def test_same_seed_same_draws_other_seed_other_draws(setup):
    sampler, _, _, _ = setup
    a = sampler.draws(2_000, 24, 20261017)
    b = sampler.draws(2_000, 24, 20261017)
    c = sampler.draws(2_000, 24, 20261018)
    assert np.array_equal(a, b) and not np.array_equal(a, c)


def test_extension_continues_each_path_with_p21(setup):
    sampler, labels, p21, _ = setup
    sampler.draws(30_000, 12, 5)
    last = sampler.last_states.copy()
    ext = sampler.extend(1, 6)
    nxt = labels[ext[:, 0]]
    for s in range(3):
        freq = np.bincount(nxt[last == s], minlength=3) / max(1, (last == s).sum())
        assert np.abs(freq - p21[s]).max() < 0.03
    assert sampler.extend(0, 6).shape == (30_000, 0)


def test_range_draws_use_only_that_range_or_refuse_a_thin_pool(setup):
    sampler, labels, _, _ = setup
    d = sampler.range_draws(150, N_BLOCKS, 1_000, 12, np.random.default_rng(1), min_pool=8)
    assert d is not None and (d >= 150).all() and (d < N_BLOCKS).all()
    assert sampler.range_draws(0, 20, 1_000, 12, np.random.default_rng(1), min_pool=8) is None


def test_chain_statistics_are_counted_from_the_chains():
    chains = np.array([[2, 2, 2, 0, 0], [2, 2, 0, 2, 2], [0, 1, 2, 2, 2], [1, 1, 1, 1, 1]], dtype=np.int8)
    s = chain_stats(chains)
    assert s["long_crisis_share"] == 0.5 and s["long_crisis_blocks"] == 3
    assert s["month_shares"] == [0.2, 0.3, 0.5]                  # 4, 6 and 10 of 20 months


def test_blocks_take_the_regime_of_their_first_day():
    m = load_regime_model()
    starts = np.array(["2008-09-12", "2008-09-15", "2017-06-15"], dtype="datetime64[D]")
    assert list(label_blocks(starts, m)) == list(m.labels_on(starts))


def test_int32_extension_gives_exactly_the_int64_draws(setup):
    """The levers' extra months are int32 to save memory; stacked onto the main draws they are the int64 values."""
    sampler, _, _, _ = setup
    draws = sampler.draws(2_000, 120, seed=11)
    extra = sampler.extend(60, seed=12)
    assert draws.dtype == np.int64 and extra.dtype == np.int32
    longer = np.hstack([draws, extra])
    assert longer.dtype == np.int64 and np.array_equal(longer[:, 120:], extra.astype(np.int64))
    assert sampler.extend(0, seed=12).dtype == np.int32


def test_rebuilding_the_pools_on_demand_gives_identical_draws(setup):
    """With relabel the sampler frees its pools and last regimes after the main draws and rebuilds them; every draw
    (main, levers' extension, both history halves) is exactly what the sampler that keeps them produces."""
    _, labels, p21, start = setup
    keep = RegimeSampler(regime_pools(labels), start, p21)
    lazy = RegimeSampler(regime_pools(labels), start, p21, relabel=lambda: labels.copy())
    for s in (keep, lazy):
        s.out = [s.draws(3_000, 90, seed=21), s.extend(48, seed=22)]
        rng = np.random.default_rng(23)
        s.out += [s.range_draws(lo, hi, 3_000, 90, rng, 8) for lo, hi in ((0, 150), (150, N_BLOCKS))]
    assert lazy.pools is None and lazy.last_states is None and lazy.extendable
    assert keep.stats == lazy.stats
    for a, b in zip(keep.out, lazy.out):
        assert a is not None and a.dtype == b.dtype and np.array_equal(a, b)


def test_a_second_main_draw_after_the_pools_were_freed_is_the_same(setup):
    """With relabel, draws() frees the pools; calling it again rebuilds them instead of failing."""
    _, labels, p21, start = setup
    lazy = RegimeSampler(regime_pools(labels), start, p21, relabel=lambda: labels.copy())
    first = lazy.draws(2_000, 60, seed=31)
    assert lazy.pools is None
    assert np.array_equal(lazy.draws(2_000, 60, seed=31), first)
