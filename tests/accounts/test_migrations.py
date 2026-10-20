"""The SQL migrations: the erase and retention functions in force must cover every table that holds a user's rows.

Several migrations replace delete_my_data() and purge_expired() (002_what_if, 005_onboarding, 006_onchain, 007_erase_everything).
The one that sorts LAST is the version in force when the files are run in order. Before 007 each version listed only the tables
known when it was written, so running 006 after 005 left the onboarding answers behind and 005 after 006 left the advisory
records, the seals and the picture behind. This test reads the files and fails when a table with a user_id is missing."""

import re
from pathlib import Path

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "supabase" / "migrations").glob("*.sql"), key=lambda p: p.name)
TABLE = re.compile(r"create table if not exists public\.(\w+)\s*\((.*?)\n\);", re.S | re.I)
# The account itself stays after "erase my data" (the user can sign in again); delete_my_account() removes it by cascade.
KEPT_BY_DESIGN = {"profiles"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tables() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in MIGRATIONS:
        for name, body in TABLE.findall(_read(path)):
            found[name] = body
    return found


def _in_force(function: str) -> tuple[Path, str]:
    """The last migration (by file name) that defines the function, and its body."""
    definition = re.compile(r"create or replace function public\." + function + r"\(\).*?\bend \$\$;", re.S | re.I)
    last = None
    for path in MIGRATIONS:
        for match in definition.finditer(_read(path)):
            last = (path, match.group(0))
    assert last is not None, f"{function}() is not defined by any migration"
    return last


def test_the_table_scan_finds_the_tables():
    tables = _tables()
    assert {"trades", "reports", "consents", "what_if_runs", "onboarding", "advisory_records", "record_seals"} <= set(tables)


def test_delete_my_data_in_force_covers_every_table_with_a_user_id():
    path, body = _in_force("delete_my_data")
    missing = [name for name, columns in _tables().items()
               if "user_id" in columns and name not in KEPT_BY_DESIGN and f"public.{name}" not in body]
    assert not missing, f"delete_my_data() as defined by {path.name} leaves rows in: {missing}; run/extend 007_erase_everything.sql last"
    assert "avatar" in body, f"{path.name} does not clear the profile picture"


def test_purge_expired_in_force_covers_every_table_with_an_expiry():
    path, body = _in_force("purge_expired")
    missing = [name for name, columns in _tables().items() if "expires_at" in columns and f"public.{name}" not in body]
    assert not missing, f"purge_expired() as defined by {path.name} never purges: {missing}"


def test_the_version_in_force_is_the_last_migration():
    """007 is meant to be last: a later migration may only replace it with a version that also passes the tests above."""
    erase_path, _ = _in_force("delete_my_data")
    purge_path, _ = _in_force("purge_expired")
    assert erase_path.name >= "007" and purge_path.name >= "007", (erase_path.name, purge_path.name)


def test_only_the_owner_may_purge_and_users_may_erase():
    path, _ = _in_force("delete_my_data")
    text = _read(path)
    assert "revoke all on function public.delete_my_data(), public.purge_expired() from public, anon, authenticated" in text
    assert "grant execute on function public.delete_my_data() to authenticated" in text
    assert "grant execute on function public.purge_expired()" not in text
