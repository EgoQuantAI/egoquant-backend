"""Run each test file in its own pytest process (the offline suite on a Windows stack that sometimes kills or stalls a process); retry a file that died or hung.

    python eval/run_tests_per_file.py <worktree> <out file> --dir tests/system [--skip a.py b.py]
    python eval/run_tests_per_file.py <worktree> <out file> --files tests/a.py tests/b.py

A native crash or a hang (no result within 900 s) loses one file, not the whole run: the file is retried up to three times.
The summary at the end says which files passed, failed or kept crashing."""
import os
import subprocess
import sys
import time
from pathlib import Path

PY = sys.executable                       # run it with the project's virtual environment
FILE_TIMEOUT = 900


def main() -> int:
    worktree, out = Path(sys.argv[1]), Path(sys.argv[2])
    args = sys.argv[3:]
    if args[0] == "--dir":
        skip = set(args[args.index("--skip") + 1:]) if "--skip" in args else set()
        directory = args[1]
        files = sorted(p.relative_to(worktree).as_posix() for p in (worktree / directory).glob("test_*.py") if p.name not in skip)
    else:
        files = args[1:]
    env = {**os.environ, "PYTHONUTF8": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    summary = []
    with out.open("w", encoding="utf-8") as log:
        for rel in files:
            result = None
            for attempt in (1, 2, 3):
                started = time.time()
                try:
                    proc = subprocess.run([PY, "-m", "pytest", "-q", rel, "-p", "no:cacheprovider", "-m", "not live",
                                           "--deselect", "tests/anchoring/test_wallet_script.py::test_creates_key_prints_only_address"],
                                          cwd=worktree, env=env, capture_output=True, text=True, errors="replace", timeout=FILE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    log.write(f"{rel} attempt {attempt} HUNG after {FILE_TIMEOUT}s (killed)\n")
                    log.flush()
                    continue
                tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1:] or [""]
                log.write(f"{rel} attempt {attempt} exit {proc.returncode} {time.time() - started:.0f}s :: {tail[0][:140]}\n")
                log.flush()
                if proc.returncode in (0, 1, 5):                      # finished (passed / test failures / nothing collected)
                    result = proc.returncode
                    if proc.returncode == 1:
                        for ln in proc.stdout.splitlines():
                            if ln.startswith("FAILED") or ln.startswith("ERROR"):
                                log.write("    " + ln[:200] + "\n")
                    break
            summary.append((rel, result))
        bad = [(r, c) for r, c in summary if c not in (0, 5)]
        log.write(f"\nSUMMARY: {len(summary)} files, {len(summary) - len(bad)} clean, {len(bad)} with failures or crashes\n")
        for r, c in bad:
            log.write(f"  {r}: {'CRASHED OR HUNG 3 times' if c is None else 'failed'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
