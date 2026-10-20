"""Local server for the hostile-user checks: the oct19 app on the committed price snapshot (no network, no keys).
usage: serve.py <worktree> <port>.  Harness-only knob: HARNESS_PORTFOLIO_LIMIT raises the 20/min build limit so
concurrent rounds are not stopped by the limiter (the limiter itself is checked separately)."""
import os
import sys

root, port = sys.argv[1], int(sys.argv[2])
os.chdir(root)
sys.path.insert(0, root)
os.environ.update({"EGOQUANT_PRICE_MODE": "snapshot", "EGOQUANT_BRIEFING": os.environ.get("EGOQUANT_BRIEFING", "fixture"),
                   "EGOQUANT_NEWS_WORKER": "off", "EGOQUANT_WARM": "off"})
for key in ("TIINGO_API_KEY", "TIINGO_WARM_API_KEY", "OPENAI_API_KEY", "EGOQUANT_DATA"):
    os.environ.pop(key, None)

import egoquant_routes  # noqa: E402

egoquant_routes._PORTFOLIO_LIMIT = int(os.environ.get("HARNESS_PORTFOLIO_LIMIT", "20"))

import faulthandler  # noqa: E402

if os.environ.get("HARNESS_STACKS"):                                  # every 45 s: where is every thread? (diagnosing a stall)
    faulthandler.dump_traceback_later(45, repeat=True, file=open(os.environ["HARNESS_STACKS"], "w"))

import uvicorn  # noqa: E402
from main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", workers=1)
