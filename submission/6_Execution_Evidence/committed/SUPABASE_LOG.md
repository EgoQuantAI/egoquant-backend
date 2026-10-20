# Database setup and security checks (Supabase)

Dated record of how the account database was set up and checked, in time order (Dubai time). No keys, tokens,
passwords or database addresses appear here.

### 19 October 2026, 03:34

- Migration `001_accounts.sql` and the privilege hardening `002_hardening.sql` were applied in one run.
- Privilege hardening (002): all privileges were revoked from the `anon` and `authenticated` roles, then granted back
  exactly as needed: SELECT on every table; DELETE only on `trades`, `cash_flows` and `reports`; INSERT and UPDATE
  only on named columns (so creation dates and expiry dates always take their defaults, and a stored trade-check
  verdict cannot be rewritten); `purge_expired()` is not callable by users.
- The daily purge job was scheduled with `pg_cron` at 03:17 UTC; saved data is kept for 180 days.
- `supabase/tests/rls_isolation.sql` passed: one user cannot read or change another user's rows. It runs in a
  rolled-back transaction, so nothing is kept.
- Anonymous Data API probe (public key, no user token): every table refused (401 / 42501). The
  `username_available` function answers, by design.
- Auth settings: email is the only sign-in method, anonymous sign-in is off, the minimum password length is 10,
  and "Confirm email" is on.
- Found: a leftover table `public.connection_test`, not part of the migrations and readable by anyone.

### 19 October 2026, 17:17

- Migration `003_avatar.sql`: profile pictures. A picture is either one of five presets or a real JPEG, PNG or WebP
  image of at most 64 kB; SVG is never accepted. A CHECK constraint enforces this in the database.
- `rls_isolation.sql` (extended for pictures) passed.

### 20 October 2026

- Migrations `002_what_if.sql` to `007_erase_everything.sql` were applied; 007 is the single final definition of
  "delete my data" and of the expiry purge, covering every table that holds user data.
- The leftover `public.connection_test` table was dropped; the public key now gets 404 for it.
- Migration `008_seal_v2.sql` (08:56): one column for the sealed report of the on-chain seal. `rls_onchain.sql`
  was re-run and passed, including the check that a user cannot change a sealed report.
- Final state: migrations 001 to 008 applied; row-level security on all 12 tables, 31 policies; `rls_isolation.sql`
  and `rls_onchain.sql` both pass (each in a rolled-back transaction, nothing kept); anonymous access refused on all
  12 tables.

### Accepted risks

- Usernames can be checked for availability (a yes/no answer), so a username's existence can be confirmed.
- The activity log is written from the user's own account: entries cannot be changed or deleted by the user, but a
  user could add entries of their own. It is not a tamper-proof audit log.
