"""scripts/onchain_new_wallet.py against a temporary .env: prints the address only, never the key."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("eth_account")              # skip without it; it is in requirements.txt
from eth_account import Account

SCRIPT = Path(__file__).parents[2] / "scripts" / "onchain_new_wallet.py"


def run(*args, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


def key_in(env: Path) -> str:
    return next(l.split("=", 1)[1] for l in env.read_text(encoding="utf-8").splitlines() if l.startswith("EGOQ_SEAL_PRIVATE_KEY="))


def test_creates_key_prints_only_address(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1")                                            # no trailing newline
    out = run("--env", str(env), cwd=tmp_path)
    assert out.returncode == 0
    key = key_in(env)
    address = Account.from_key(key).address
    assert out.stdout.strip() == address
    assert key.removeprefix("0x") not in out.stdout + out.stderr
    assert env.read_text(encoding="utf-8").startswith("OTHER=1\nEGOQ_SEAL_PRIVATE_KEY=0x")
    assert oct(os.stat(env).st_mode & 0o777) == "0o600"
    assert run("--env", str(env), "--address", cwd=tmp_path).stdout.strip() == address


def test_refuses_to_overwrite_and_replace_is_explicit(tmp_path):
    env = tmp_path / ".env"
    run("--env", str(env), cwd=tmp_path)
    first = key_in(env)
    again = run("--env", str(env), cwd=tmp_path)
    assert again.returncode == 3 and key_in(env) == first
    assert first.removeprefix("0x") not in again.stdout + again.stderr
    assert run("--env", str(env), "--replace", cwd=tmp_path).returncode == 0
    assert key_in(env) != first and env.read_text(encoding="utf-8").count("EGOQ_SEAL_PRIVATE_KEY=") == 1


def test_refuses_an_env_git_would_commit(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    out = run("--env", str(tmp_path / ".env"), cwd=tmp_path)
    assert out.returncode == 2 and not (tmp_path / ".env").exists()
    (tmp_path / ".gitignore").write_text(".env\n")
    assert run("--env", str(tmp_path / ".env"), cwd=tmp_path).returncode == 0
