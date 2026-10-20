"""Supabase access for user accounts: verify the signed-in user, then read and write AS that user.

Every call carries the user's own access token, so Supabase's Row-Level Security (user_id = auth.uid(),
supabase/migrations/001_accounts.sql) applies on top of the user_id filter this module adds itself. There is
no service-role key here or anywhere in the backend: the backend can never read another user's rows, even
by mistake. Passwords never reach this code (sign-up and login go from the frontend to Supabase Auth).

Environment: SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_KEY; a service-role/secret key is refused) (the public "anon"/publishable key; RLS protects the data).
Without them the account endpoints answer 503 and the rest of the API works as before.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

import requests

TIMEOUT = (3.0, 10.0)                  # connect, read (seconds)
SIGNUP_CONSENTS_KEY = "eq_consents"    # user_metadata: {scope: version} ticked on the sign-up screen (frontend sets it)
USER_CACHE_SECONDS = 60.0              # a verified token is trusted this long before asking Supabase again


class AccountError(Exception):
    """User-safe: `status` is the HTTP status to answer with; the message never contains a token or a row."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None
    name: str | None = None                                # what the person asked to be called (sign-up metadata)
    signup_consents: tuple[tuple[str, str], ...] = ()      # (scope, version) ticked at sign-up, not yet recorded

    @staticmethod
    def from_auth(data: Mapping[str, Any]) -> "User":
        """From Supabase's /auth/v1/user answer. Only our own user_metadata keys are read, and they are checked."""
        meta = data.get("user_metadata") if isinstance(data.get("user_metadata"), dict) else {}
        name = meta.get("name")
        name = (" ".join(name.split())[:40] or None) if isinstance(name, str) else None
        pending = meta.get(SIGNUP_CONSENTS_KEY)
        consents = tuple(sorted((str(k), str(v)) for k, v in pending.items()
                                if isinstance(k, str) and isinstance(v, str))) if isinstance(pending, dict) else ()
        return User(id=str(data["id"]), email=data.get("email"), name=name, signup_consents=consents)


class AccountStore(Protocol):
    def user(self, token: str) -> User: ...
    def select(self, token: str, table: str, params: Mapping[str, str]) -> list[dict]: ...
    def insert(self, token: str, table: str, rows: list[dict]) -> list[dict]: ...
    def update(self, token: str, table: str, params: Mapping[str, str], values: dict) -> list[dict]: ...
    def delete(self, token: str, table: str, params: Mapping[str, str]) -> int: ...
    def rpc(self, token: str | None, name: str, args: dict) -> Any: ...
    def update_user_data(self, token: str, data: dict) -> None: ...


class SupabaseStore:
    """PostgREST + Auth over HTTPS with the user's token. `session` is for tests."""

    def __init__(self, url: str, anon_key: str, session: requests.Session | None = None):
        if not url.startswith("https://"):
            raise AccountError(503, "Accounts are not configured correctly (Supabase URL must be https).")
        self.url, self.anon_key = url.rstrip("/"), anon_key
        self.session = session or requests.Session()
        self._users: dict[str, tuple[float, User]] = {}        # sha256(token) -> (time, user); never the token
        self._lock = Lock()

    # ---- auth --------------------------------------------------------------------------------
    def user(self, token: str) -> User:
        if not token:
            raise AccountError(401, "Please sign in.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._users.get(digest)
            if hit and now - hit[0] < USER_CACHE_SECONDS:
                return hit[1]
        try:
            data = self._request("GET", "/auth/v1/user", token)
        except AccountError as error:
            if error.status == 403:                            # the Auth service refuses a token it cannot read
                raise AccountError(401, "Your session has expired. Please sign in again.") from None
            raise
        if not isinstance(data, dict) or not data.get("id"):
            raise AccountError(401, "Your session has expired. Please sign in again.")
        user = User.from_auth(data)
        with self._lock:
            self._users = {k: v for k, v in self._users.items() if now - v[0] < USER_CACHE_SECONDS}
            self._users[digest] = (now, user)
        return user

    # ---- data, always as the user -------------------------------------------------------------
    def select(self, token: str, table: str, params: Mapping[str, str]) -> list[dict]:
        return self._request("GET", f"/rest/v1/{table}", token, params=dict(params)) or []

    def insert(self, token: str, table: str, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        return self._request("POST", f"/rest/v1/{table}", token, json=rows,
                             headers={"Prefer": "return=representation"}) or []

    def update(self, token: str, table: str, params: Mapping[str, str], values: dict) -> list[dict]:
        return self._request("PATCH", f"/rest/v1/{table}", token, params=dict(params), json=values,
                             headers={"Prefer": "return=representation"}) or []

    def delete(self, token: str, table: str, params: Mapping[str, str]) -> int:
        rows = self._request("DELETE", f"/rest/v1/{table}", token, params=dict(params),
                             headers={"Prefer": "return=representation"}) or []
        return len(rows)

    def rpc(self, token: str | None, name: str, args: dict) -> Any:
        return self._request("POST", f"/rest/v1/rpc/{name}", token, json=args)

    def update_user_data(self, token: str, data: dict) -> None:
        """Merge keys into the user's OWN user_metadata (Supabase Auth, as the user; null removes a key)."""
        self._request("PUT", "/auth/v1/user", token, json={"data": data})
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            self._users.pop(digest, None)              # the cached user carries the old metadata

    # ---- transport ----------------------------------------------------------------------------
    def _request(self, method: str, path: str, token: str | None, *, params=None, json=None, headers=None):
        auth = token or self.anon_key                  # rpc(None, ...) = anonymous (username_available only)
        hdrs = {"apikey": self.anon_key, "Authorization": f"Bearer {auth}", "Content-Type": "application/json",
                **(headers or {})}
        try:
            r = self.session.request(method, self.url + path, params=params, json=json, headers=hdrs,
                                     timeout=TIMEOUT)
        except requests.RequestException:
            raise AccountError(503, "The account service is unreachable. Please try again.") from None
        if r.status_code == 401:
            raise AccountError(401, "Your session has expired. Please sign in again.")
        if r.status_code == 403:                       # Row-Level Security refused the row
            raise AccountError(403, "Not allowed.")
        if r.status_code == 409:
            raise AccountError(409, "That already exists.")
        if r.status_code >= 400:                       # never pass Supabase's text through: it can quote input
            raise AccountError(502, "The account service could not complete the request.")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()


_store: AccountStore | None = None
_store_lock = Lock()


def _is_privileged_key(key: str) -> bool:
    """A service-role / secret key bypasses Row-Level Security. Only the public anon/publishable key may be used."""
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("role") == "service_role"
        except (ValueError, TypeError):
            return False
    return False


def get_store() -> AccountStore:
    """The process's account store, built from the environment on first use (tests replace it)."""
    global _store
    with _store_lock:
        if _store is None:
            url = os.getenv("SUPABASE_URL", "").strip()
            key = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
            if not url or not key:
                raise AccountError(503, "Accounts are not configured on this server.")
            if _is_privileged_key(key):             # RLS would be bypassed: refuse to run with it
                raise AccountError(503, "Accounts are not configured on this server.")
            _store = SupabaseStore(url, key)
        return _store


def set_store(store: AccountStore | None) -> None:
    global _store
    with _store_lock:
        _store = store
