"""Create the sealing wallet: a NEW key, written into the backend .env, never shown.

    .venv/bin/python scripts/onchain_new_wallet.py            # creates the key, prints ONLY the address
    .venv/bin/python scripts/onchain_new_wallet.py --address  # prints the address of the key already in .env

The key goes into EGOQ_SEAL_PRIVATE_KEY in the backend .env (git-ignored, owner-only). It is never printed,
logged or returned. The script refuses to run if .env is not git-ignored, and refuses to overwrite a key that is
already there (--replace does that on purpose, e.g. after the key was exposed; old seals stay valid because
/verify accepts every published sealing address).

The wallet holds only free Sepolia test ETH. Fund the printed address from a Sepolia faucet.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

VAR = "EGOQ_SEAL_PRIVATE_KEY"
LINE = re.compile(rf"^\s*(export\s+)?{VAR}\s*=", re.M)
DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"


def _git_ignored(env: Path) -> bool:
    """True if git ignores `env`. Outside a git repository there is nothing to commit it to, so True."""
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=env.parent,
                                capture_output=True, text=True)
        if inside.returncode != 0:
            return True
        return subprocess.run(["git", "check-ignore", "-q", env.name], cwd=env.parent).returncode == 0
    except FileNotFoundError:                       # no git installed: cannot check, refuse
        return False


def _key_from_env_file(env: Path) -> str | None:
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if LINE.match(line):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def main(argv: list[str] | None = None) -> int:
    from eth_account import Account

    ap = argparse.ArgumentParser(description="Create the EgoQuant sealing wallet (prints only the address).")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV, help="the .env file to write (default: backend .env)")
    ap.add_argument("--replace", action="store_true", help="replace an existing key (after an exposure)")
    ap.add_argument("--address", action="store_true", help="only print the address of the existing key")
    args = ap.parse_args(argv)
    env: Path = args.env.expanduser().resolve()

    existing = _key_from_env_file(env)
    if args.address:
        if not existing:
            print(f"No {VAR} in {env}.", file=sys.stderr)
            return 1
        try:
            print(Account.from_key(existing).address)
        except Exception:                           # never echo the value or the library's message
            print(f"{VAR} in {env} is not a valid key.", file=sys.stderr)
            return 1
        return 0

    if not _git_ignored(env):
        print(f"Refusing: {env} is not git-ignored. Add it to .gitignore first.", file=sys.stderr)
        return 2
    if existing and not args.replace:
        print(f"Refusing: {VAR} is already in {env}. Use --address to see its address, "
              "or --replace to make a new key on purpose.", file=sys.stderr)
        return 3

    acct = Account.create()
    line = f"{VAR}={acct.key.hex() if acct.key.hex().startswith('0x') else '0x' + acct.key.hex()}\n"
    text = env.read_text() if env.exists() else ""
    if existing:
        text = LINE.sub(lambda _m: "\0", text, count=1)     # mark the old line, then swap it whole
        text = "\n".join(line.rstrip("\n") if ln.startswith("\0") else ln for ln in text.split("\n"))
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(env, 0o600)
    del line, text
    print(acct.address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
