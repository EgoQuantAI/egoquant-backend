# SUPABASE log (branch supabase/setup)

Append only, newest at the bottom. Written by the SUPABASE session working with Bideci. No keys, tokens,
passwords, JWTs or database URLs appear here, only variable names.

### 2026-10-19 03:09 Dubai

**Read so far (nothing changed yet):** supabase/migrations/001_accounts.sql, supabase/tests/rls_isolation.sql,
docs/SECURITY.md, egoquant/accounts/store.py + routes.py, egoquant_routes.py (/account/portfolio,
/account/trade-check), frontend src/lib/server-auth.ts, src/app/api/auth/*, src/app/api/account/*.

**State**
- Branch `supabase/setup` created from origin/october18 @ a80fc88.
- `supabase/tests/live_smoke.py` is **NOT on origin/october18 yet**. The SUPABASE_KEY fallback in store.py **is**
  there. I'm continuing with the other steps.
- Backend `.env` on Bideci's machine has SUPABASE_URL (https, *.supabase.co) and SUPABASE_KEY. I checked the key
  type without printing it: it's a **publishable** key (`sb_publishable_…`), not secret or service_role. OK.
- Frontend `.env.local` has only BACKEND_URL so far. NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY
  are missing (step 4). Both repos' .gitignore files cover these env files (`git check-ignore` confirms).

**Plan:** the 8 steps in the brief, in order. I'll leave 001 alone if it runs cleanly. Fixes go into a new
idempotent `supabase/migrations/002_hardening.sql` so 001 stays the reference Onur wrote, plus a corrected
rls_isolation.sql.

**Problems found in the SQL (proposed fixes, to be applied in 002 unless Onur objects)**
1. **rls_isolation.sql always fails (a bug in the test).** As user B it asserts
   `count(*) from profiles = 0`, but B can see B's own profile (`rls_test_b`), so the count is 1 and the test raises
   "RLS BROKEN: B sees A's profile" even when RLS is correct. Fix: count rows where `user_id <> auth.uid()`.
   I'll also extend it: all 7 tables checked both ways, B can't update A's trade_check, anon can read none of
   the 7 tables, and authenticated can't call purge_expired.
2. **The authenticated role keeps ALL privileges.** Supabase's default privileges grant ALL on new public
   tables (and EXECUTE on new functions) to anon and authenticated. 001 revokes from anon but only *adds*
   grants for authenticated, so authenticated still has DELETE/UPDATE/TRUNCATE/REFERENCES/TRIGGER on every table.
   RLS still blocks row operations that have no policy. TRUNCATE ignores RLS, though PostgREST can't send it.
   The same default also leaves `purge_expired()` callable by any signed-in user over RPC.
   Fix: `revoke all … from anon, authenticated`, re-grant exactly what 001 intends, and
   `revoke execute on purge_expired(), handle_new_user() from authenticated`.
3. **A user can rewrite their own trade-check verdict.** The update policy on trade_checks allows any
   column, so anyone holding their own token plus the public key can PATCH `verdict`/`result`/`planned` straight
   through PostgREST. That undercuts "verdicts come from deterministic code". The backend only ever updates
   `user_decision, decided_at`. Fix: column-level `grant update (user_decision, decided_at)`. profiles:
   the backend never updates them, so update becomes `(username)` only (user_id and created_at frozen).
4. **Users can set their own retention dates.** Inserts accept a client-supplied `expires_at`
   (and created_at, given_at, at), so a user can keep rows past 180 days or backdate the log. The backend
   never sends these columns (checked report_to_rows, routes, egoquant_routes). Fix: column-level INSERT
   grants without those columns, so they always take their defaults.
   No table, column or function is renamed or removed in 1–4. These are privilege changes only, and every
   call the backend makes today keeps working. **Announced here before applying.**

**Accepted risks to note in SECURITY.md (no change proposed)**
- `username_available` is callable by anon straight through PostgREST with the public key, which bypasses the
  backend's 60/min limit. Usernames can be enumerated (yes/no only). This was accepted by design.
- operation_log is insert-only for the user (no update/delete), but a user can add made-up entries for
  themselves directly. It's not a tamper-proof audit log.

**Settings decision:** I'll set Supabase's minimum password length to **10**, not 8, because the frontend
enforces `PASSWORD_MIN = 10` (server-auth.ts) and the two should agree.

**Need from Onur**
- Push `supabase/tests/live_smoke.py` (Umut's backend push) to origin/october18. Step 5 is blocked until then.
- Possible store.py issue, to verify in step 5: anonymous rpc sends `Authorization: Bearer <publishable key>`.
  Supabase's new `sb_publishable_` keys aren't JWTs. If PostgREST rejects that, the fix is to leave out the
  Authorization header when there's no user token (store.py `_request`, `auth = token or self.anon_key`).
- FYI (frontend, U7): `/api/account/[[...path]]` has no routes yet for `trade-check` and
  `trade-checks/:id/decision`, so the trade check can't be reached from the browser.
- FYI: with "Confirm email" OFF, Supabase's /signup returns a session, but the frontend ignores it and shows
  "confirm your email". Demo accounts will have to log in once by hand after sign-up. That's fine.
- Please confirm or veto fixes 2–4 (privilege tightening in 002_hardening.sql). Unless a DECISION says
  otherwise, I'll apply them after 001 and the corrected RLS test pass.

### 2026-10-19 03:34 Dubai

**Done (Supabase dashboard, through Bideci's browser)**
- `create extension if not exists pg_cron;` then `001_accounts.sql` + new `002_hardening.sql`, in one SQL editor
  run → `Success. No rows returned`. pg_cron is **enabled**. Job `egoquant-purge-expired @ 17 3 * * *` (03:17 UTC
  daily) is scheduled.
- Corrected `supabase/tests/rls_isolation.sql` → **PASS** `RLS isolation: all checks passed (nothing was kept)`.
- Verification query (read-only):
  - PASS RLS enabled on all 7 tables (profiles, consents, trades, cash_flows, reports, trade_checks, operation_log)
  - PASS anon has no grant on any of the 7 tables
  - PASS authenticated has table-level SELECT on all 7 and DELETE on trades/cash_flows/reports only; INSERT/UPDATE
    are column-level (002)
  - PASS trigger `on_auth_user_created` on auth.users
  - PASS 0 leftover test users
- Data API probe from the terminal with the publishable key, no user token:
  - PASS all 7 tables → 401 / 42501 (permission denied)
  - PASS `rpc/username_available` → 200, so `public` is exposed to the Data API
- **Resolved (no backend change needed):** store.py sends `Authorization: Bearer <publishable key>` for
  anonymous rpc. Supabase accepts this (200), the same as apikey alone.

**Schema/privilege changes applied** (announced in entry 1; no table/column/function renamed or removed):
`supabase/migrations/002_hardening.sql`
- revoke ALL from anon + authenticated on the 7 tables, then grant back:
  - SELECT on all 7
  - DELETE on trades, cash_flows, reports
  - UPDATE (username) on profiles
  - UPDATE (user_decision, decided_at) on trade_checks
  - INSERT on the data columns only (no created_at/given_at/at/expires_at)
- EXECUTE on purge_expired/handle_new_user revoked from authenticated too
- pg_cron schedule repeated (idempotent)

Every write the backend makes (routes.py, egoquant_routes.py) fits these grants. I checked each insert's keys.

**Auth settings**
- PASS Email is the only provider enabled. Anonymous sign-ins OFF, manual linking OFF, sign-ups allowed.
- PASS Site URL `http://localhost:3000` (it was already set)
- PASS Redirect URLs `http://localhost:3000/**` added. The existing Vercel URL entry is kept.
- PASS Minimum password length 6 → **10** (matches frontend PASSWORD_MIN). Character requirements left at
  default (none).
- **"Confirm email" is still ON.** Turning it off was blocked for me as a security-weakening action, so Bideci
  turns it off by hand for local testing. I'll record here when it's off and when it's back on.
- **BLOCKER (accepted):** the email templates **can't be edited on the free plan without custom SMTP**, so
  the `/api/auth/confirm?token_hash=…` link can't be set. The default link goes to Supabase's /verify, which
  confirms the email and redirects to the Site URL with tokens in the URL fragment. The frontend doesn't read
  fragments, so the user lands on the homepage **not signed in**, but the email IS confirmed and login works.
  For the demo, accounts are created with "Confirm email" OFF. After it's back ON, new users click the link,
  then log in.

**Found:** `public.connection_test`, a table not in our migrations, holds 1 row and is **readable and writable
by anon** through the Data API (full default grants). Someone probably created it to test the connection. I
haven't touched it. Recommend `drop table public.connection_test;` (or at least enabling RLS on it) once its
owner confirms. Waiting for a decision from Bideci/Onur.

**Env (names only):** frontend `.env.local` now has NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY
(the same publishable key the backend uses). Backend `.env` has SUPABASE_URL, SUPABASE_KEY and SUPABASE_ANON_KEY.
All of these files are gitignored. Frontend BACKEND_URL still points at Railway; it has to be switched to
http://localhost:8000 for steps 5–6 (Bideci's call).

**Need from Onur**
- `supabase/tests/live_smoke.py` is still not on origin/october18. Step 5 is blocked.
- Optional: have the frontend read the URL fragment after email confirmation (or show "Email confirmed,
  please log in"), since the template link can't be changed without SMTP.
- Decide on `public.connection_test` (drop it).

### 2026-10-19 17:17 Dubai

**Profile pictures (requested by Bideci).** Schema change, announced here and already applied:
`supabase/migrations/003_avatar.sql` adds one nullable column, `profiles.avatar` (text). It's either
`preset:p1`..`preset:p5` (five built-in pictures drawn by the frontend) or a JPEG/PNG/WebP data URL of at most
90,000 characters, enforced by a CHECK constraint. It also grants `update (username, avatar)` on profiles to
authenticated. Nothing is renamed or removed.
- Applied in the Supabase SQL editor → `Success`.
- PASS `rls_isolation.sql` (extended: A sets their own picture, an SVG data URL is refused by the CHECK, and B can't
  change A's picture) → `RLS isolation: all checks passed (nothing was kept)`.

**Backend** (`egoquant/accounts/routes.py`, changed at Bideci's request; Bideci owns the frontend and backend):
- `GET /account/profile` returns username + avatar (one small read for the sidebar).
- `PUT /account/avatar` checks the value with `valid_avatar()`: a preset, or base64 that decodes to real
  JPEG/PNG/WebP bytes (magic bytes checked), at most 64 kB. Never SVG. Rejected values are never echoed back.
- `GET /account/me` now also returns `avatar`.
- Tests: `tests/accounts/test_avatar.py` (presets and real images accepted; SVG, fake PNG, mislabelled type,
  oversize and bad base64 refused; A vs B; a smuggled user_id → 422; a token is required).

**Need from Onur:** a SECURITY.md line for uploads. Suggested: "Profile pictures: presets or a 160 px JPEG made in
the browser; the backend accepts only real JPEG/PNG/WebP bytes ≤ 64 kB (magic bytes), never SVG; the database
CHECK repeats the rule; RLS keeps each picture on its own profile row; deleting the account deletes it."

### 2026-10-20 07:40 Dubai (Bideci's AI, with Bideci's permission: read-only audit + RLS proofs; counts and names only)

**Migrations: all of 001 to 007 are applied** (the log above stopped at 003). Evidence, from one read-only query in the SQL editor:
- 12 of 12 tables in `public` (profiles, consents, trades, cash_flows, reports, trade_checks, operation_log, what_if_runs, privacy_log,
  onboarding, advisory_records, record_seals); RLS on for all 12; 31 policies.
- `authenticated` grants equal exactly the grants in 002_hardening, 003, 005_onboarding, 005_what_if_hardening and 006 (table SELECT on
  the 12; DELETE only on trades, cash_flows, reports; column INSERT/UPDATE only where the migrations grant them; what_if_runs and
  privacy_log: SELECT + column INSERT only).
- 003: `profiles.avatar` exists. 004: `replace_report_briefing` exists; 006 version (knows the seals).
- 007: `erase_covers_both` = true; `purge_expired` covers onboarding and the on-chain tables; no table with `user_id` is missing from
  `delete_my_data()`; `purge_expired` is executable by none of anon/authenticated/public; `delete_my_data` by authenticated only.
- Retention default: `now() + 180 days`.
- pg_cron: job `egoquant-purge-expired`, `17 3 * * *`, `select public.purge_expired()`, active; 1 run so far (19 Oct 03:17 UTC), succeeded.
- Rows: profiles 5, consents 10, trades 0, cash_flows 0, reports 7, trade_checks 0, operation_log 30, what_if_runs 0, privacy_log 0,
  onboarding 4, advisory_records 2, record_seals 4.

**RLS proofs (run in the SQL editor, each one transaction rolled back; checked afterwards: 0 test users, 0 test rows, 0 open transactions):**
- PASS `supabase/tests/rls_isolation.sql` -> "RLS isolation: all checks passed (nothing was kept)".
- PASS `supabase/tests/rls_onchain.sql` -> "RLS on-chain: all checks passed (nothing was kept)", after a fix to the TEST: its setup
  inserted `reports (id, ...)` and `advisory_records (id, ...)` as the signed-in user, which the column grants rightly refuse
  ("permission denied for table reports"). The rows with fixed ids are now made by the table owner before the test switches to the
  users; every check is unchanged.

**Open gap:** `public.connection_test` still exists and `anon` holds ALL privileges on it (it is the only anon grant in `public`;
readable through the Data API with the public key: HTTP 200). It holds one row ("EgoQuant database connected"); only
`scripts/testsupa.py` reads it. Runbook step 11 says drop it: `drop table if exists public.connection_test;` (left for a human to run;
the AI's attempt was stopped by its own safety guard).

**Data API probe with the public key only (A6):** all 12 tables answer 401 (42501 permission denied); `rpc/username_available` 200 (by design).

**Auth settings (read only, nothing changed):** Confirm email ON; new sign-ups allowed; anonymous sign-ins OFF; manual linking OFF;
providers: Email only; minimum password length 10; leaked-password check not available (Pro plan only); email OTP expiry 3600 s;
Site URL = the Vercel URL; redirect list = the Vercel URL and `http://localhost:3000/**`.
### 2026-10-20 07:10 Dubai (Bideci ran it; checked by Bideci's AI)
- `drop table if exists public.connection_test;` run by Bideci in the SQL editor. Check with the public key: `GET /rest/v1/connection_test` now
  answers 404 (it was 200 with the row). It held the only `anon` grant the audit found in `public`, so that gap is closed.

### 2026-10-20 08:56 Dubai (Bideci's AI, on Onur's go at 08:54)
- Applied `supabase/migrations/008_seal_v2.sql` (Aslan's seal v2) in the SQL editor: success. Check: `advisory_records.sealed_document` exists,
  `text`, nullable. Additive only: no policy and no erase change, so 007 was not re-run.
- Re-ran `supabase/tests/rls_onchain.sql` (one transaction, rolled back) with one extra check, "a signed-in user cannot UPDATE `sealed_document`":
  **PASS** ("RLS on-chain: all checks passed (nothing was kept)").
