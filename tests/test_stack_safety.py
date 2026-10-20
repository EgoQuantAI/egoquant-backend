"""Stack safety of the server process (20 Oct): one math-library thread by default, and the library versions are visible."""
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from main import app

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


def child(env_extra: dict) -> list[str]:
    """Import main in a fresh process and print what the three variables are."""
    env = {k: v for k, v in os.environ.items() if k not in NAMES}
    env.update(env_extra)
    env["PYTHONUTF8"] = "1"
    code = "import main, os; print(*[os.environ.get(n) for n in %r])" % (NAMES,)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    return out.stdout.split()


def test_the_math_libraries_default_to_one_thread():
    assert child({}) == ["1", "1", "1"]


def test_a_value_set_in_the_environment_wins():
    assert child({"OPENBLAS_NUM_THREADS": "4"}) == ["4", "1", "1"]


def test_data_status_shows_the_library_versions():
    libraries = TestClient(app).get("/data/status").json()["libraries"]
    assert set(libraries) == {"pandas", "numpy", "scipy"}
    assert all(isinstance(v, str) and v[0].isdigit() for v in libraries.values())
