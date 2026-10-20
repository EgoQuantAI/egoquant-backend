"""Live smoke test of the account system against a REAL Supabase project and a RUNNING backend.

    SUPABASE_URL=...  SUPABASE_ANON_KEY=...  BACKEND_URL=http://localhost:8000  python supabase/tests/live_smoke.py

Two ways to get the two users (never both):
  * PRE-CREATED (recommended; keeps "Confirm email" ON): two accounts that were signed up on the site and confirmed (the dashboard's
    "Add user" does not work: the new-user trigger needs the username from the sign-up form), passed as SMOKE_A_EMAIL,
    SMOKE_A_PASSWORD, SMOKE_B_EMAIL, SMOKE_B_PASSWORD. The script only logs in, no e-mail is sent, and it NEVER deletes these
    accounts (it does erase their saved data at the end). Nothing about them is printed.
  * SELF-CREATED: needs "Confirm email" switched OFF for the run (otherwise sign-up cannot log in at once). It creates two
    throw-away users (random usernames), and deletes both accounts at the end.
It checks access control THROUGH the backend and directly against Supabase's REST API. Prints PASS/FAIL lines; never prints a
key, token, e-mail or password. Exit code 0 = everything passed. Uses only the public anon key. Written without access to a
project: if a check fails because of how the dashboard is set up, fix the script or the setup and say so in the log.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import requests

try:        # the backend's own git-ignored .env supplies SUPABASE_URL and SUPABASE_ANON_KEY on the team PC (values already set win)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY", "")
BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
SAMPLE = Path(__file__).resolve().parents[2] / "tests" / "agent_b" / "data" / "ibkr_cautious_seller.xml"
TABLES = ("profiles", "consents", "trades", "cash_flows", "reports", "trade_checks", "operation_log",
          "what_if_runs", "privacy_log", "onboarding", "advisory_records", "record_seals")      # every table of the migrations
PRECREATED = {k: os.environ.get(f"SMOKE_{k}", "") for k in ("A_EMAIL", "A_PASSWORD", "B_EMAIL", "B_PASSWORD")}
USE_PRECREATED = all(PRECREATED.values())
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def auth(path: str, body: dict, token: str | None = None) -> requests.Response:
    return requests.post(f"{URL}/auth/v1/{path}", json=body, timeout=20,
                         headers={"apikey": ANON, "Authorization": f"Bearer {token or ANON}"})


def api(method: str, path: str, token: str | None = None, **kw) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.request(method, f"{BACKEND}{path}", headers=headers, timeout=60, **kw)


def new_user(tag: str) -> tuple[str, str]:
    """(username, access_token). Sign-up with the username as metadata, exactly as the app does."""
    name = f"smoke_{tag}_{secrets.token_hex(3)}"
    email, password = f"{name}@example.com", secrets.token_urlsafe(18)
    r = auth("signup", {"email": email, "password": password, "data": {"username": name}})
    check(f"sign-up {tag}", r.status_code in (200, 201), f"HTTP {r.status_code}")
    r = auth("token?grant_type=password", {"email": email, "password": password})
    token = r.json().get("access_token", "") if r.status_code == 200 else ""
    check(f"login {tag} (needs Confirm email OFF)", bool(token), f"HTTP {r.status_code}")
    return name, token


def existing_user(tag: str) -> tuple[str, str]:
    """(username, access_token) of a pre-created user: log in only. Nothing about the account is printed."""
    r = auth("token?grant_type=password", {"email": PRECREATED[f"{tag.upper()}_EMAIL"],
                                           "password": PRECREATED[f"{tag.upper()}_PASSWORD"]})
    token = r.json().get("access_token", "") if r.status_code == 200 else ""
    check(f"login {tag} (pre-created user)", bool(token), f"HTTP {r.status_code}")
    me = api("GET", "/account/me", token) if token else None
    return (me.json().get("username", "") if me is not None and me.status_code == 200 else ""), token


def main() -> int:
    if not URL or not ANON:
        print("Set SUPABASE_URL and SUPABASE_ANON_KEY (names only here; never paste values in a chat).")
        return 2
    check("backend is up", api("GET", "/health").status_code == 200)
    # 1. the anonymous role sees nothing in any table
    for table in TABLES:
        r = requests.get(f"{URL}/rest/v1/{table}?select=*&limit=1", timeout=20,
                         headers={"apikey": ANON, "Authorization": f"Bearer {ANON}"})
        check(f"anon cannot read {table}", r.status_code in (401, 403) or (r.status_code == 200 and r.json() == []),
              f"HTTP {r.status_code}")
    if USE_PRECREATED:
        a_name, a = existing_user("a")
        b_name, b = existing_user("b")
    else:
        a_name, a = new_user("a")
        b_name, b = new_user("b")
    if not (a and b):
        print("Cannot continue without two logged-in users.")
        return 1
    if not USE_PRECREATED:                       # a sign-up attempt sends e-mail and spends the shared quota: only self-created runs
        dup = auth("signup", {"email": f"dup_{secrets.token_hex(3)}@example.com", "password": secrets.token_urlsafe(18),
                              "data": {"username": a_name}})
        check("a taken username is refused at sign-up", dup.status_code >= 400, f"HTTP {dup.status_code}")
    if a_name:
        check("username check says taken", api("GET", f"/account/username-available?name={a_name}").json()
              == {"available": False})
    # 2. through the backend
    check("/account/me needs sign-in", api("GET", "/account/me").status_code == 401)
    me = api("GET", "/account/me", a)
    check("/account/me returns the username", me.status_code == 200 and bool(me.json().get("username")),
          f"HTTP {me.status_code}")
    version = me.json().get("consent_version", "") if me.status_code == 200 else ""
    for scope in ("ibkr_data", "trade_check"):
        r = api("POST", "/account/consent", a, json={"scope": scope, "version": version})
        check(f"consent {scope}", r.status_code == 200, f"HTTP {r.status_code}")
    imported = api("POST", "/account/trades/import", a,
                   json={"ibkr_report": SAMPLE.read_text(encoding="utf-8"), "ibkr_filename": SAMPLE.name})
    check("A imports the sample IBKR report", imported.status_code == 200, f"HTTP {imported.status_code}")
    n_a = len(api("GET", "/account/trades", a).json().get("trades", []))
    check("A sees the imported trades", n_a > 0, str(n_a))
    check("B sees none of A's trades", api("GET", "/account/trades", b).json().get("trades") == [])
    # 3. directly against Supabase with B's token: still nothing of A's
    for table in TABLES:
        r = requests.get(f"{URL}/rest/v1/{table}?select=*", timeout=20,
                         headers={"apikey": ANON, "Authorization": f"Bearer {b}"})
        ids = {row.get("user_id") or row.get("id") for row in r.json()} if r.status_code == 200 else set()
        check(f"B sees only B's rows in {table} (RLS)", r.status_code == 200 and len(ids) <= 1, f"HTTP {r.status_code}")
    forged = requests.post(f"{URL}/rest/v1/trades", timeout=20, headers={
        "apikey": ANON, "Authorization": f"Bearer {b}", "Content-Type": "application/json"},
        json={"user_id": "00000000-0000-0000-0000-000000000000", "trade_date": "2026-01-02", "symbol": "VTI",
              "side": "buy", "quantity": 1, "price": 1, "currency": "USD", "asset_category": "STK",
              "cash_change": -1, "source": "user_recorded"})
    check("B cannot write a row for another user_id", forged.status_code >= 400, f"HTTP {forged.status_code}")
    # 4. erase
    check("A deletes the data", api("DELETE", "/account/data", a).status_code == 200)
    check("A's trades are gone", api("GET", "/account/trades", a).json().get("trades") == [])
    if USE_PRECREATED:
        print("Pre-created accounts are kept (the script never deletes an account it did not create).")
    else:
        check("A deletes the account", api("DELETE", "/account", a).status_code == 200)
        check("B deletes the account", api("DELETE", "/account", b).status_code == 200)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
