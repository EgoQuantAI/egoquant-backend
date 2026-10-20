# Bideci / SUPABASE Claude: finish in one sitting (Onur, Mon 19 Oct)
Goal: every account feature proven against a REAL Supabase project, then Confirm email back ON. Nothing here needs a key in chat or in the repo.
Get the latest code first: `git fetch`; work from `origin/oct19` (it now holds 002_hardening, live_smoke.py, the body-size limit, the cash-need change). Keep your branch `supabase/setup`; never push oct19.

## 1. SQL, in this order (Supabase SQL editor; each file must finish without errors)
1. `supabase/migrations/001_accounts.sql`
2. `supabase/migrations/002_hardening.sql`
3. `supabase/migrations/002_what_if.sql`
4. `supabase/migrations/003_avatar.sql` (profile picture column)
5. `supabase/migrations/004_report_briefing.sql` (function that lets a saved report take Agent D's final news)
6. `supabase/migrations/005_onboarding.sql` (Bideci's: the onboarding answers kept in the account; one table)
7. `supabase/migrations/005_what_if_hardening.sql` (final; any order with the other 005 and with 006). Without it the two what-if tables keep UPDATE/DELETE/TRUNCATE for signed-in users.
8. `supabase/migrations/006_onchain.sql` (Aslan's on-chain sealing tables)
9. `supabase/migrations/007_erase_everything.sql`, ALWAYS LAST (re-run it after any other migration). 002_what_if, 005_onboarding and 006_onchain each redefine `delete_my_data()` and `purge_expired()` with only the tables they knew, so the one run last would leave the other's rows behind ("erase my data" would not erase the onboarding answers, or the records, seals and picture). 007 lists every table; `tests/accounts/test_migrations.py` fails when a table with a `user_id` is missing from it.
10. `supabase/tests/rls_isolation.sql` and `rls_onchain.sql`: the table list is 7 in 001, 9 with the what-if tables, 10 with onboarding, 12 with the two on-chain tables (all have `user_id`, none is `connection_test`). They must end without errors.
11. Drop `public.connection_test` (not ours; anon-writable): `drop table if exists public.connection_test;`
12. Run the two grant checks at the bottom of the 005_what_if_hardening file and the check at the bottom of the 007 file (`erase_covers_both` must be true, the purge list empty); paste only the row counts in the log.

## 2. Dashboard settings (names only in the log)
- Auth: Site URL `http://localhost:3000` (add the deployed URL later), redirect `http://localhost:3000/**`, password minimum 10, email sign-up the only provider, "Confirm email" OFF while testing only.
- Data API: schema `public` exposed, RLS ON for all 12 tables, anon reads nothing.
- Keys: only the anon/publishable key in `SUPABASE_ANON_KEY` (backend `.env`) and `NEXT_PUBLIC_SUPABASE_*` (frontend `.env.local`). No service-role/secret key anywhere (the backend refuses one).

## 3. Live smoke test
Start the backend (`.venv/Scripts/python -m uvicorn main:app --port 8000`), then
`SUPABASE_URL=... SUPABASE_ANON_KEY=... python supabase/tests/live_smoke.py` (values from the .env, never echoed). Goal: every line PASS.
Add to the script (your lane): anon cannot read `what_if_runs` / `privacy_log`; B cannot read A's rows in them; B cannot insert a row for A's `user_id`; B cannot UPDATE or DELETE a row of its own (403/permission denied); an insert with a forged `expires_at` is refused (column not granted).
The consent version moved to `2026-10-19` (the script reads it from `/account/me`, so nothing to change).

## 4. By hand in the browser (npm run dev, backend on 8000 for you)
Sign up, log in, accept both consents, import `tests/agent_b/data/ibkr_cautious_seller.xml`, see the trades on /account, build a report while signed in (saved report), run a trade check and press "I made this trade", run one What-If, log out/in, delete my data, delete my account. Write down any page + message that breaks (do not fix `egoquant/**`; log it under "Need from Onur").

## 5. Judges' accounts, then Confirm email ON
Create the demo/judge accounts through the sign-up page while Confirm email is OFF (credentials to Savas privately, never in the log), then switch Confirm email back ON and say so in the log. The default confirmation email lands on the homepage signed out: that is accepted (free plan, no custom SMTP).

## 6. Final log entry (append-only, docs/supabase/SUPABASE_LOG.md)
What is set up (settings list, no secrets), smoke result (PASS/FAIL lines), remaining risks, and a 5-line security note for docs/SECURITY.md. Push `supabase/setup`; Onur merges it into oct19.
