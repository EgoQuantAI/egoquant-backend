"""Does reading ONE SnapshotPriceSource from many threads at once crash or hang pandas?  usage: snaprace.py <worktree> <rounds> [fix]
Each round: a fresh source (cold lazy index tables, like the first requests after a start), 12 threads behind a barrier, each doing what a
build does: fetch_adjusted_close(...), hash_pandas_object, copy.  A crash shows as an access violation / exit code, a hang as the watchdog."""
import faulthandler
import random
import sys
import threading
import time

root, rounds = sys.argv[1], int(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else ""
sys.path.insert(0, root)
faulthandler.dump_traceback_later(int(__import__("os").environ.get("WATCHDOG", "150")), exit=True)

import pandas as pd  # noqa: E402
print("pandas", pd.__version__, "numpy", __import__("numpy").__version__, flush=True)

from egoquant.agents.market_intel.snapshot import SnapshotPriceSource, YAHOO_SNAPSHOT_PATH  # noqa: E402

if mode == "lock":                                       # the proposed fix: one read of the shared frame at a time
    _lock = threading.Lock()
    _orig = SnapshotPriceSource.fetch_adjusted_close

    def _locked(self, tickers, start, end):
        with _lock:
            return _orig(self, tickers, start, end)

    SnapshotPriceSource.fetch_adjusted_close = _locked

tickers_all = None
done = 0
t0 = time.time()
for r in range(rounds):
    src = SnapshotPriceSource(YAHOO_SNAPSHOT_PATH)
    if mode == "warm":                                   # the proposed fix: build every lazy table once, before sharing
        src.prices.columns.get_indexer(list(src.prices.columns[:3]))
        src.prices.index.get_indexer(src.prices.index[:3])
        src.prices._mgr  # noqa: B018
    tickers_all = list(src.prices.columns)
    barrier = threading.Barrier(12)
    errors = []

    def work(i):
        rng = random.Random(r * 100 + i)
        barrier.wait()
        for _ in range(6):
            picks = rng.sample(tickers_all, rng.randint(5, 60))
            start = pd.Timestamp("2018-01-01") + pd.Timedelta(days=rng.randint(0, 1500))
            frame = src.fetch_adjusted_close(picks, start.date(), pd.Timestamp("2026-10-16").date())
            pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
            frame.copy().mean()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    done += 1
print(f"{done} rounds of 12 threads finished without a crash in {time.time() - t0:.0f}s")
